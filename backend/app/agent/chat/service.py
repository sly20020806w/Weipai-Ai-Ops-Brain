"""对话输入和回答只从持久化事实投影，不在 HTTP 请求内调查或执行。"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.chat.models import ChatAnswer, ChatSubmission
from app.agent.investigation import AgentConclusion
from app.agent.reviewer.models import ReviewDecision
from app.db.base import utc_now
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.tasks.console_queries import ConsoleConflict, ConsoleNotFound
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.states import TaskSource, TaskStatus
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventReceipt, NormalizedEvent
from app.triggers.service import EventService


async def chat_input(session: AsyncSession, task_id: UUID) -> tuple[Evidence, ChatSubmission]:
    record = await session.scalar(
        select(Evidence).where(Evidence.task_id == task_id, Evidence.source_tool == "chat.request")
    )
    if record is None:
        raise ConsoleNotFound("对话任务不存在")
    value = ChatSubmission.model_validate_json(json.dumps(record.result_snapshot))
    event = await session.scalar(select(OpsEvent).where(OpsEvent.task_id == task_id))
    if (
        event is None
        or event.source != TaskSource.HUMAN.value
        or event.origin != "manual"
        or event.external_id != f"chat:{value.input.request_id}"
        or event.service_name != value.input.service_name
    ):
        raise ConsoleConflict("对话输入与 OpsEvent 身份不匹配")
    return record, value


async def submit_chat(session: AsyncSession, value: ChatSubmission) -> EventReceipt:
    value = ChatSubmission.model_validate(value)
    receipt = (
        await EventService(session).accept(
            [
                NormalizedEvent(
                    origin="manual",
                    source=TaskSource.HUMAN,
                    external_id=f"chat:{value.input.request_id}",
                    service_name=value.input.service_name,
                    title="AI 对话：" + " ".join(value.input.message.split())[:490],
                    occurred_at=utc_now(),
                )
            ]
        )
    )[0]
    ledger = LedgerService(session)
    records = await ledger.evidence_for_task(UUID(receipt.task_id))
    saved = next((e for e in records if e.source_tool == "chat.request"), None)
    snapshot = value.model_dump(mode="json")
    if saved is not None:
        if saved.result_snapshot != snapshot:
            raise ConsoleConflict("相同 request_id 的对话内容不能改变")
    else:
        if value.input.previous_task_id is not None:
            _, previous = await chat_input(session, value.input.previous_task_id)
            if (
                previous.actor != value.actor
                or previous.input.service_name != value.input.service_name
            ):
                raise ConsoleConflict("追问必须属于同一操作人和服务")
            answer = await read_answer(session, value.input.previous_task_id)
            if answer.answer is None:
                raise ConsoleConflict("上一轮尚无有效回答")
        saved = await ledger.append_evidence(
            task_id=UUID(receipt.task_id),
            source_tool="chat.request",
            parameters={"request_id": str(value.input.request_id)},
            result_snapshot=snapshot,
        )
        await ledger.append_audit(
            task_id=UUID(receipt.task_id),
            event_type=AuditEventType.HUMAN_INTERACTION,
            actor=value.actor,
            operation="chat.submit",
            outcome="accepted",
            evidence_id=saved.id,
            details={"mode": value.input.mode},
        )
    return receipt


async def validate_conclusion(
    session: AsyncSession, task_id: UUID, record: Evidence
) -> AgentConclusion:
    if record.task_id != task_id or record.source_tool != "agent.conclusion":
        raise ConsoleConflict("回答结论身份无效")
    conclusion = AgentConclusion.model_validate_json(json.dumps(record.result_snapshot))
    ledger = LedgerService(session)
    audits = await ledger.audits_for_task(task_id)
    for reference in conclusion.evidence_ids:
        fact = await ledger.get_evidence(reference)
        if fact.task_id != task_id or not any(
            a.event_type is AuditEventType.TOOL_CALL
            and a.actor == "codex-main-agent"
            and a.operation == fact.source_tool
            and a.evidence_id == reference
            and a.outcome == "succeeded"
            and a.details.get("mode") == "live"
            for a in audits
        ):
            raise ConsoleConflict("回答引用必须是同任务主 Agent 成功查询的真实证据")
    return conclusion


def render_answer(conclusion: AgentConclusion) -> str:
    return "\n".join(
        claim.statement + " " + " ".join(f"[Evidence:{ref}]" for ref in claim.evidence_ids)
        for claim in (conclusion.root_cause, *conclusion.findings)
    ) + ("\n待核实：" + "；".join(conclusion.uncertainties) if conclusion.uncertainties else "")


async def read_answer(session: AsyncSession, task_id: UUID) -> ChatAnswer:
    await chat_input(session, task_id)
    task = await session.get(AITask, task_id)
    assert task is not None
    records = await LedgerService(session).evidence_for_task(task_id)
    conclusion = next((e for e in reversed(records) if e.source_tool == "agent.conclusion"), None)
    review = next((e for e in reversed(records) if e.source_tool == "reviewer.verdict"), None)
    plan = next((e for e in reversed(records) if e.source_tool == "action_plan"), None)
    terminal = task.status in {
        TaskStatus.CLOSED,
        TaskStatus.ESCALATED,
        TaskStatus.AUTOMATION_ABORTED,
    }
    ready = task.status is TaskStatus.CLOSED or (
        plan is not None
        and task.status
        in {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.WAITING_INFORMATION,
            TaskStatus.EXECUTING,
            TaskStatus.VERIFYING,
            TaskStatus.RESOLVED,
            TaskStatus.LEARNING,
        }
    )
    if not ready or conclusion is None or review is None:
        return ChatAnswer(task_id=task_id, status=task.status.value, pending=not terminal)
    accepted = await validate_conclusion(session, task_id, conclusion)
    checked = ReviewDecision.model_validate_json(json.dumps(review.result_snapshot))
    if checked.report.verdict != "clear" or checked.conclusion_evidence_id != conclusion.id:
        raise ConsoleConflict("回答缺少对应结论的有效复核")
    accepted = checked.conclusion
    decision = None
    if plan is not None:
        decision = ActionPlan.model_validate_json(json.dumps(plan.result_snapshot)).decision.value
    return ChatAnswer(
        task_id=task_id,
        status=task.status.value,
        pending=False,
        answer=render_answer(accepted),
        evidence_ids=tuple(sorted(accepted.evidence_ids)),
        conclusion_evidence_id=conclusion.id,
        review_evidence_id=review.id,
        plan_evidence_id=plan.id if plan else None,
        policy_decision=decision,
    )
