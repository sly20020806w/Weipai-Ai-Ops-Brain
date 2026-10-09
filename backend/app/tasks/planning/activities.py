"""PLANNING Activity：复核门禁、模型检查点、证据校验与只追加计划。"""

import hashlib
import json
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.activities import DatabaseInvestigationIO, lock_task
from app.agent.client import LLMClient
from app.agent.fake import FakeLLM, ScriptedChatStep, create_llm_client
from app.agent.investigation import AgentConclusion, InvestigationSpec
from app.agent.models import ChatRequest
from app.agent.reviewer.models import ReviewDecision
from app.config import Settings
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.lifecycle import task_runbook_context
from app.runbooks.workflow_models import spec_hash
from app.tasks.planning.engine import evaluate_plan, parse_draft, planning_chat
from app.tasks.planning.models import ActionPlan, PlanningRequest, PlanningResult
from app.tasks.planning.scenario import payment_plan_response
from app.tasks.review_gate import require_review_for_planning
from app.tasks.states import TaskStatus
from app.tools.registry import ToolRegistry, json_object


def planning_result(evidence_id: UUID, plan: ActionPlan) -> PlanningResult:
    # JSONB 会重排对象键；原调用与重试统一返回规范序列化，Temporal 结果保持稳定。
    return PlanningResult(
        str(evidence_id),
        json.dumps(
            plan.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


class PlanningActivities:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        llm_factory: Callable[[ChatRequest], LLMClient] | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.llm_factory = llm_factory or (
            lambda request: (
                FakeLLM([ScriptedChatStep(payment_plan_response)])
                if settings.llm_mode == "fake"
                else create_llm_client(settings)
            )
        )

    @activity.defn(name="task.plan_actions")
    async def plan(self, request: PlanningRequest) -> PlanningResult:
        try:
            if request.task.status is not TaskStatus.PLANNING:
                raise ValueError("只有 PLANNING 可生成处置计划")
            spec = InvestigationSpec.model_validate_json(request.spec_json)
            async with self.database.session() as session:
                ledger = LedgerService(session)
                async with session.begin():
                    task = await lock_task(session, request.task)
                    await require_review_for_planning(session, task)
                    conclusion = await ledger.get_evidence(UUID(request.conclusion_evidence_id))
                    review = await ledger.get_evidence(UUID(request.review_evidence_id))
                    for record, source in (
                        (conclusion, "agent.conclusion"),
                        (review, "reviewer.verdict"),
                    ):
                        if (
                            record.task_id != task.id
                            or record.source_tool != source
                            or record.parameters.get("phase_version") != request.task.version - 1
                        ):
                            raise ValueError("规划输入不是紧邻当前 PLANNING 的结论和复核")
                    original = AgentConclusion.model_validate_json(
                        json.dumps(conclusion.result_snapshot)
                    )
                    decision = ReviewDecision.model_validate_json(
                        json.dumps(review.result_snapshot)
                    )
                    if (
                        decision.conclusion_evidence_id != conclusion.id
                        or decision.report.verdict != "clear"
                    ):
                        raise ValueError("当前结论未通过复核")
                    if review.parameters.get("spec_hash") != spec_hash(request.spec_json):
                        raise ValueError("规划服务与时间窗必须匹配当前 Reviewer 输入")
                    references = original.evidence_ids | decision.report.evidence_ids
                    audits = await ledger.audits_for_task(task.id)
                    facts: list[dict[str, object]] = []
                    for reference in sorted(references, key=str):
                        item = await ledger.get_evidence(reference)
                        if item.task_id != task.id or not any(
                            audit.event_type is AuditEventType.TOOL_CALL
                            and audit.evidence_id == reference
                            and audit.operation == item.source_tool
                            and audit.outcome == "succeeded"
                            for audit in audits
                        ):
                            raise ValueError("计划引用缺少同任务真实成功查询")
                        # 服务与时间窗由查询证据再次检查，不能篡改目标服务生成新计划。
                        if item.parameters.get("service_name") not in {None, spec.service_name}:
                            raise ValueError("规划服务与原调查不符")
                        facts.append(
                            {
                                "evidence_id": str(reference),
                                "source": item.source_tool,
                                "snapshot": item.result_snapshot,
                            }
                        )
                    chat = planning_chat(spec, decision, facts)
                    runbook = await task_runbook_context(
                        session, task.id, self.settings.runbook_maturity_config
                    )
                    key = json_object(
                        {
                            "phase_version": request.task.version,
                            "policy_hash": hashlib.sha256(
                                json.dumps(
                                    {
                                        "environment": self.settings.app_env,
                                        "config": self.settings.policy_config.model_dump(
                                            mode="json"
                                        ),
                                        **(
                                            {"runbook": runbook.model_dump(mode="json")}
                                            if runbook
                                            else {}
                                        ),
                                    },
                                    sort_keys=True,
                                ).encode()
                            ).hexdigest(),
                            "request_hash": DatabaseInvestigationIO(
                                session,
                                ToolRegistry(),
                                self.settings,
                                request.task,
                                self.llm_factory,
                            ).key(1, chat.model_dump_json())["request_hash"],
                            "conclusion_evidence_id": request.conclusion_evidence_id,
                            "review_evidence_id": request.review_evidence_id,
                        }
                    )
                    previous = await session.scalar(
                        select(Evidence).where(
                            Evidence.task_id == task.id,
                            Evidence.source_tool == "action_plan",
                            Evidence.parameters["phase_version"].as_integer()
                            == request.task.version,
                        )
                    )
                    if previous is not None:
                        if previous.parameters != key:
                            raise ValueError("同一规划版本输入发生冲突")
                        plan = ActionPlan.model_validate_json(json.dumps(previous.result_snapshot))
                        return planning_result(previous.id, plan)
                io = DatabaseInvestigationIO(
                    session,
                    ToolRegistry(),
                    self.settings,
                    request.task,
                    self.llm_factory,
                    checkpoint_prefix="planner",
                    actor="codex-main-agent",
                )
                response = await io.think(1, chat)
                plan = evaluate_plan(
                    parse_draft(response),
                    request,
                    spec,
                    references,
                    create_policy_engine(self.settings),
                    runbook,
                )
                async with session.begin():
                    await lock_task(session, request.task)
                    current_runbook = await task_runbook_context(
                        session, UUID(request.task.task_id), self.settings.runbook_maturity_config
                    )
                    if current_runbook != runbook:
                        raise ValueError("规划期间 Runbook 成熟度变化，需要重新规划")
                    previous = await session.scalar(
                        select(Evidence).where(
                            Evidence.task_id == UUID(request.task.task_id),
                            Evidence.source_tool == "action_plan",
                            Evidence.parameters["phase_version"].as_integer()
                            == request.task.version,
                        )
                    )
                    snapshot = json_object(plan.model_dump(mode="json"))
                    if previous is not None:
                        if previous.parameters != key or previous.result_snapshot != snapshot:
                            raise ValueError("同一规划版本结果发生冲突")
                        return planning_result(previous.id, plan)
                    accepted = await ledger.append_evidence(
                        task_id=UUID(request.task.task_id),
                        source_tool="action_plan",
                        parameters=key,
                        result_snapshot=snapshot,
                    )
                    return planning_result(accepted.id, plan)
        except (ValueError, LookupError):
            raise ApplicationError(
                "处置计划或引用证据被拒绝", type="InvalidActionPlan", non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("处置计划生成失败") from None
