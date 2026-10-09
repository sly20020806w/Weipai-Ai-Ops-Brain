"""工单 Activity 注册；重试与人工暂停由统一 Temporal Workflow 负责。"""

import json
from uuid import UUID

from temporalio import activity

from app.agent.activities import lock_task
from app.agent.investigation import AgentConclusion, InvestigationResult
from app.agent.workflow_models import ConclusionRequest
from app.config import Settings
from app.connectors.ops_platform.tickets import TicketState
from app.db.session import Database
from app.executor.tickets import TicketExecutor
from app.learning.tickets import TicketLearning
from app.ledger.service import LedgerService
from app.tasks.planning.models import PlanningResult
from app.tasks.states import TaskStatus
from app.tasks.tickets.models import (
    TicketAnalysisRequest,
    TicketExecutionRequest,
    TicketPlanRequest,
    TicketStageRequest,
    TicketVerifyRequest,
    TicketVerifyResult,
)
from app.tasks.tickets.service import TicketStore, make_ticket_plan
from app.tools.models import DispatchResult
from app.tools.registry import json_object
from app.verifier.tickets import TicketVerifier


class TicketActivities:
    def __init__(
        self, database: Database, settings: Settings, *, state: TicketState | None = None
    ) -> None:
        state = state or TicketState()
        self.store = TicketStore(database, settings, state)
        self.executor = TicketExecutor(database, settings, state)
        self.verifier = TicketVerifier(database, settings, state)
        self.learning = TicketLearning(database, settings, state)

    @activity.defn(name="ticket.prepare")
    async def prepare(self, value: TicketStageRequest) -> str:
        return await self.store.prepare(value)

    @activity.defn(name="ticket.investigate")
    async def investigate(self, value: TicketAnalysisRequest) -> InvestigationResult:
        return await self.store.investigate(value)

    @activity.defn(name="ticket.conclude")
    async def conclude(self, request: ConclusionRequest) -> str:
        if request.task.status is not TaskStatus.RCA:
            raise ValueError("工单结论必须在 RCA")
        async with self.store.database.session() as session, session.begin():
            task = await lock_task(session, request.task)
            ledger = LedgerService(session)
            records = await ledger.evidence_for_task(task.id)
            observed = sorted(
                (
                    e
                    for e in records
                    if e.source_tool == "ticket.agent.observe"
                    and e.parameters.get("phase_version") == task.status_version - 1
                ),
                key=lambda e: int(str(e.parameters["step"])),
            )
            ids = [
                str(r.evidence_id)
                for e in observed
                if (
                    r := DispatchResult.model_validate_json(json.dumps(e.result_snapshot))
                ).evidence_id
            ]
            conclusion = AgentConclusion.model_validate_json(request.result.conclusion_json)
            audits = await ledger.audits_for_task(task.id)
            if ids != request.result.observed_ids or not conclusion.evidence_ids <= {
                UUID(i) for i in ids
            }:
                raise ValueError("工单结论引用未观察或伪造证据")
            for reference in conclusion.evidence_ids:
                fact = await ledger.get_evidence(reference)
                if fact.task_id != task.id or not any(
                    a.evidence_id == reference
                    and a.actor == "codex-main-agent"
                    and a.operation == fact.source_tool
                    and a.outcome == "succeeded"
                    for a in audits
                ):
                    raise ValueError("工单结论缺少成功查询审计")
            previous = [
                e
                for e in records
                if e.source_tool == "ticket.conclusion"
                and e.parameters.get("phase_version") == task.status_version
            ]
            if previous:
                if previous[0].result_snapshot != conclusion.model_dump(mode="json"):
                    raise ValueError("工单结论重投发生冲突")
                return str(previous[0].id)
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="ticket.conclusion",
                parameters={"phase_version": task.status_version},
                result_snapshot=json_object(conclusion.model_dump(mode="json")),
            )
            return str(evidence.id)

    @activity.defn(name="ticket.review")
    async def review(self, value: TicketPlanRequest) -> str:
        return await self.store.review(value)

    @activity.defn(name="ticket.plan")
    async def plan(self, value: TicketPlanRequest) -> PlanningResult:
        async with self.store.database.session() as session, session.begin():
            return await make_ticket_plan(session, self.store.settings, value)

    @activity.defn(name="ticket.execute")
    async def execute(self, value: TicketExecutionRequest) -> str:
        return await self.executor.execute(value)

    @activity.defn(name="ticket.verify")
    async def verify(self, value: TicketVerifyRequest) -> TicketVerifyResult:
        return await self.verifier.verify(value)

    @activity.defn(name="ticket.learn")
    async def learn(self, value: TicketStageRequest) -> str:
        return await self.learning.learn(value)
