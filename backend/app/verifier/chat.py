"""独立核验只读回答的证据链；不表示生产问题已修复。"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.chat.models import ChatVerification, ChatVerifyRequest
from app.agent.chat.service import chat_input, validate_conclusion
from app.config import Settings
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import RiskLevel
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.review_gate import require_review_for_planning
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor, VerificationRequired
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus, ToolModel
from app.tools.registry import ToolRegistry
from app.verifier.authority import _verification_scope


class ChatVerificationQuery(ToolModel):
    task_id: UUID
    verifying_version: int
    conclusion_evidence_id: UUID


async def validate_answer_facts(session: AsyncSession, task: AITask, conclusion_id: UUID) -> None:
    _, submission = await chat_input(session, task.id)
    if submission.input.mode != "question":
        raise VerificationRequired("仅只读对话可以用回答核验结束任务")
    ledger = LedgerService(session)
    conclusion = await ledger.get_evidence(conclusion_id)
    if conclusion.parameters.get("phase_version") != task.status_version - 3:
        raise VerificationRequired("回答结论版本失效")
    await validate_conclusion(session, task.id, conclusion)
    await require_review_for_planning(session, task)
    if any(
        e.source_tool in {"action_plan", "executor.intent"}
        for e in await ledger.evidence_for_task(task.id)
    ):
        raise VerificationRequired("只读对话不能混入执行计划")


async def validate_chat_verification(
    session: AsyncSession, task: AITask, evidence: Evidence, *, require_passed: bool
) -> ChatVerification:
    result = ChatVerification.model_validate_json(json.dumps(evidence.result_snapshot))
    if (
        evidence.task_id != task.id
        or evidence.source_tool != "verify_chat"
        or result.task_id != task.id
        or result.verifying_version != task.status_version
        or (require_passed and not result.passed)
        or evidence.parameters
        != {
            "task_id": str(task.id),
            "verifying_version": task.status_version,
            "conclusion_evidence_id": str(result.conclusion_evidence_id),
        }
    ):
        raise VerificationRequired("对话独立验证身份或版本错误")
    if not any(
        a.event_type is AuditEventType.TOOL_CALL
        and a.actor == "verifier"
        and a.operation == "verify_chat"
        and a.outcome == "succeeded"
        and a.evidence_id == evidence.id
        and a.details.get("mode") == "live"
        for a in await LedgerService(session).audits_for_task(task.id)
    ):
        raise VerificationRequired("缺少对话独立验证审计")
    await validate_answer_facts(session, task, result.conclusion_evidence_id)
    return result


class ChatVerifier:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    @activity.defn(name="verifier.chat")
    async def verify(self, request: ChatVerifyRequest) -> TaskSnapshot:
        try:
            async with self.database.session() as session, session.begin():
                task = await session.scalar(
                    select(AITask).where(AITask.id == UUID(request.task.task_id)).with_for_update()
                )
                if task is None or request.task.status is not TaskStatus.VERIFYING:
                    raise VerificationRequired("只读回答验证阶段错误")
                ledger = LedgerService(session)
                query = ChatVerificationQuery(
                    task_id=task.id,
                    verifying_version=request.task.version,
                    conclusion_evidence_id=UUID(request.conclusion_evidence_id),
                )
                key = query.model_dump(mode="json")
                cached = next(
                    (
                        e
                        for e in await ledger.evidence_for_task(task.id)
                        if e.source_tool == "chat.verified" and e.parameters == key
                    ),
                    None,
                )
                if cached is not None:
                    history = await session.scalar(
                        select(TaskStatusHistory).where(
                            TaskStatusHistory.task_id == task.id,
                            TaskStatusHistory.sequence == request.task.version + 1,
                        )
                    )
                    if history is None or history.to_status is not TaskStatus.RESOLVED:
                        raise VerificationRequired("对话验证检查点缺少状态历史")
                    return TaskSnapshot(str(task.id), TaskStatus.RESOLVED, request.task.version + 1)
                if (task.status, task.status_version) != (
                    request.task.status,
                    request.task.version,
                ):
                    raise VerificationRequired("回答验证版本冲突")
                registry = ToolRegistry()

                async def verify(value: ChatVerificationQuery) -> ChatVerification:
                    if value != query:
                        raise VerificationRequired("回答验证范围错误")
                    await validate_answer_facts(session, task, value.conclusion_evidence_id)
                    return ChatVerification(**value.model_dump(), passed=True)

                registry.register(
                    name="verify_chat",
                    description="独立核验只读回答及真实查询证据",
                    input_model=ChatVerificationQuery,
                    output_model=ChatVerification,
                    handler=verify,
                    risk_level=RiskLevel.L0,
                )
                result = await ToolDispatcher(
                    registry, create_policy_engine(self.settings), ledger
                ).dispatch(
                    task_id=task.id, tool_name="verify_chat", parameters=key, actor="verifier"
                )
                if result.status is DispatchStatus.SUCCEEDED and result.evidence_id is not None:
                    with _verification_scope(task.id, task.status_version, result.evidence_id):
                        changed = await TaskService(session).transition(
                            task.id,
                            TaskStatus.RESOLVED,
                            expected_status=task.status,
                            expected_version=task.status_version,
                            actor=TransitionActor.VERIFIER,
                            reason="只读回答证据核验完成；未声明生产问题恢复或授予任何动作权限",
                        )
                    await ledger.append_evidence(
                        task_id=task.id,
                        source_tool="chat.verified",
                        parameters=key,
                        result_snapshot={"evidence_id": str(result.evidence_id)},
                    )
                    return TaskSnapshot(str(changed.id), changed.status, changed.status_version)
            raise VerificationRequired("回答独立核验未通过")
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("回答独立核验被拒绝", non_retryable=True) from None
