"""Temporal 复核 Activity，绑定当前 RCA 版本并复用已提交检查点。"""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.activities import DatabaseInvestigationIO, lock_task
from app.agent.client import LLMClient
from app.agent.fake import FakeLLM, ScriptedChatStep, create_llm_client
from app.agent.investigation import (
    AgentConclusion,
    AgentStepLimit,
    InvalidConclusion,
    InvestigationSpec,
)
from app.agent.models import ChatRequest
from app.agent.reviewer.engine import ReviewerAgent
from app.agent.reviewer.models import (
    REVIEW_ACTOR,
    REVIEW_TOOLS,
    ReviewDecision,
    ReviewInput,
    ReviewRequest,
    ReviewResult,
    adjusted_conclusion,
    within_review_scope,
)
from app.agent.reviewer.scenario import review_response
from app.config import Settings
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.runbooks.workflow_models import spec_hash
from app.tasks.states import TaskStatus
from app.tools.models import DispatchResult, DispatchStatus
from app.tools.registry import ToolRegistry, json_object
from app.tools.runtime import investigation_registry


def configured_reviewer(settings: Settings, request: ChatRequest) -> LLMClient:
    if settings.llm_mode != "fake":
        return create_llm_client(settings)
    return FakeLLM([ScriptedChatStep(review_response)])


class ReviewerActivities:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        llm_factory: Callable[[ChatRequest], LLMClient] | None = None,
        registry_factory: Callable[
            [Settings, AsyncSession], AbstractAsyncContextManager[ToolRegistry]
        ]
        | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.llm_factory = llm_factory or (lambda request: configured_reviewer(settings, request))
        self.registry_factory = registry_factory or (
            lambda configuration, session: investigation_registry(
                configuration, session, reviewer=True
            )
        )

    @activity.defn(name="reviewer.review")
    async def review(self, request: ReviewRequest) -> ReviewResult:
        try:
            if request.task.status is not TaskStatus.RCA:
                raise InvalidConclusion("Reviewer 只能复核 RCA 阶段")
            spec = InvestigationSpec.model_validate_json(request.spec_json)
            task_id = UUID(request.task.task_id)
            async with self.database.session() as session:
                ledger = LedgerService(session)
                async with session.begin():
                    await lock_task(session, request.task)
                    original = await ledger.get_evidence(UUID(request.conclusion_evidence_id))
                    if (
                        original.task_id != task_id
                        or original.source_tool != "agent.conclusion"
                        or original.parameters.get("phase_version") != request.task.version
                    ):
                        raise InvalidConclusion("复核结论不是当前任务当前 RCA 版本")
                    conclusion = AgentConclusion.model_validate_json(
                        json.dumps(original.result_snapshot)
                    )
                    snapshots = []
                    for reference in sorted(conclusion.evidence_ids, key=str):
                        referenced = await ledger.get_evidence(reference)
                        if referenced.task_id != task_id:
                            raise InvalidConclusion("原结论包含跨任务引用")
                        snapshots.append(
                            json_object(
                                {
                                    "id": str(referenced.id),
                                    "source_tool": referenced.source_tool,
                                    "parameters": referenced.parameters,
                                    "result": referenced.result_snapshot,
                                }
                            )
                        )
                    value = ReviewInput(
                        spec=spec,
                        conclusion_evidence_id=original.id,
                        conclusion=conclusion,
                        evidence_snapshots=tuple(snapshots),
                    )
                async with self.registry_factory(self.settings, session) as registry:
                    io = DatabaseInvestigationIO(
                        session,
                        registry,
                        self.settings,
                        request.task,
                        self.llm_factory,
                        checkpoint_prefix="reviewer",
                        actor=REVIEW_ACTOR,
                        allowed_tools=REVIEW_TOOLS,
                        call_scope=lambda call: within_review_scope(call, spec),
                    )
                    report, observed, steps = await ReviewerAgent().run(
                        value, io, max_steps=self.settings.agent_config.reviewer_max_steps
                    )
                async with session.begin():
                    await lock_task(session, request.task)
                    evidence = await ledger.evidence_for_task(task_id)
                    observations = sorted(
                        (
                            item
                            for item in evidence
                            if item.source_tool == "reviewer.observe"
                            and item.parameters.get("phase_version") == request.task.version
                        ),
                        key=lambda item: int(str(item.parameters["step"])),
                    )
                    stored = [
                        DispatchResult.model_validate_json(json.dumps(item.result_snapshot))
                        for item in observations
                    ]
                    if observed != [
                        str(item.evidence_id)
                        for item in stored
                        if item.status is DispatchStatus.SUCCEEDED
                    ]:
                        raise InvalidConclusion("Reviewer 观察与已提交记录不一致")
                    audits = await ledger.audits_for_task(task_id)
                    for reference in report.evidence_ids:
                        item = await ledger.get_evidence(reference)
                        if item.task_id != task_id or not any(
                            audit.event_type is AuditEventType.TOOL_CALL
                            and audit.actor == REVIEW_ACTOR
                            and audit.evidence_id == reference
                            and audit.operation == item.source_tool
                            and audit.outcome == DispatchStatus.SUCCEEDED.value
                            for audit in audits
                        ):
                            raise InvalidConclusion("Reviewer 证据缺少同任务成功调用审计")
                    decision = ReviewDecision(
                        conclusion_evidence_id=original.id,
                        original_confidence=conclusion.confidence,
                        conclusion=adjusted_conclusion(conclusion, report),
                        report=report,
                        observed_ids=tuple(UUID(item) for item in observed),
                        steps=steps,
                    )
                    parameters = json_object(
                        {
                            "phase_version": request.task.version,
                            "conclusion_evidence_id": request.conclusion_evidence_id,
                            "spec_hash": spec_hash(request.spec_json),
                        }
                    )
                    previous = await session.scalar(
                        select(Evidence).where(
                            Evidence.task_id == task_id,
                            Evidence.source_tool == "reviewer.verdict",
                            Evidence.parameters["phase_version"].as_integer()
                            == request.task.version,
                        )
                    )
                    snapshot = json_object(decision.model_dump(mode="json"))
                    if previous is not None:
                        if (
                            previous.parameters != parameters
                            or previous.result_snapshot != snapshot
                        ):
                            raise InvalidConclusion("同一 RCA 的复核结果冲突")
                        return ReviewResult(str(previous.id), decision.model_dump_json())
                    accepted = await ledger.append_evidence(
                        task_id=task_id,
                        source_tool="reviewer.verdict",
                        parameters=parameters,
                        result_snapshot=snapshot,
                    )
                    return ReviewResult(str(accepted.id), decision.model_dump_json())
        except AgentStepLimit:
            raise ApplicationError(
                "Reviewer 超过最大复核步数", type="ReviewerStepLimit", non_retryable=True
            ) from None
        except (ValueError, LookupError):
            raise ApplicationError(
                "Reviewer 复核或证据被拒绝", type="InvalidReview", non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("Reviewer 复核失败") from None
