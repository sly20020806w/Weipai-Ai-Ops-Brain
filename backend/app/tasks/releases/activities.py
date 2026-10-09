"""发布 Activity 组合根，Timer、重试和审批暂停仍由统一 Workflow 控制。"""

import json
from datetime import timedelta
from uuid import UUID

from temporalio import activity

from app.agent.activities import lock_task
from app.agent.investigation import InvestigationSpec
from app.config import Settings
from app.connectors.changes.releases import FakeReleaseState
from app.db.base import utc_now
from app.db.session import Database
from app.executor.models import ExecutionRequest, ExecutionResult
from app.executor.releases import ReleaseExecutor
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.runbooks.workflow_models import RunbookMatchRequest, RunbookMatchResult
from app.tasks.planning.models import PlanningResult
from app.tasks.releases.models import (
    ReleaseManifest,
    ReleaseObserveRequest,
    ReleaseObserveResult,
    ReleasePlanRequest,
    ReleaseStageRequest,
)
from app.tasks.releases.service import ReleaseStore
from app.tasks.states import TaskStatus
from app.tasks.tickets.service import cached
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import JsonObject
from app.tools.registry import json_object
from app.verifier.releases import ReleaseVerifier


class ReleaseActivities:
    def __init__(
        self, database: Database, settings: Settings, *, state: FakeReleaseState | None = None
    ) -> None:
        self.state = state or FakeReleaseState()
        self.store = ReleaseStore(database, settings, self.state)
        self.executor = ReleaseExecutor(database, settings, self.state)
        self.verifier = ReleaseVerifier(database, settings, self.state)

    @activity.defn(name="release.match_runbook")
    async def match_runbook(self, request: ReleaseStageRequest) -> RunbookMatchResult:
        async with self.store.database.session() as session, session.begin():
            task = await lock_task(session, request.task)
            record = await self.store.read(
                session,
                task.id,
                "get_release_request",
                {"release_id": request.release_id},
                "workflow",
            )
            manifest = ReleaseManifest.model_validate_json(json.dumps(record.result_snapshot))
        now = utc_now()
        spec = InvestigationSpec(
            service_name=manifest.service_name,
            title="发布预检查 " + request.release_id,
            start=now - timedelta(seconds=1),
            end=now,
            max_steps=20,
        )
        return await RunbookActivities(self.store.database, self.store.settings).match(
            RunbookMatchRequest(request.task, spec.model_dump_json())
        )

    @activity.defn(name="release.read_assessment")
    async def read_assessment(self, evidence_id: str) -> str:
        async with self.store.database.session() as session:
            record = await LedgerService(session).get_evidence(UUID(evidence_id))
            if record.source_tool != "release.assessment":
                raise ValueError("不是发布检查证据")
            return json.dumps(record.result_snapshot)

    @activity.defn(name="release.assess")
    async def assess(self, request: ReleaseStageRequest) -> str:
        return await self.store.assess(request)

    @activity.defn(name="release.review")
    async def review(self, request: ReleasePlanRequest) -> str:
        return await self.store.review(request)

    @activity.defn(name="release.plan")
    async def plan(self, request: ReleasePlanRequest) -> PlanningResult:
        return await self.store.plan(request)

    @activity.defn(name="release.execute")
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return await self.executor.execute(request)

    @activity.defn(name="release.observe")
    async def observe(self, request: ReleaseObserveRequest) -> ReleaseObserveResult:
        return await self.verifier.observe(request)

    @activity.defn(name="release.report")
    async def report(self, snapshot: TaskSnapshot) -> str:
        async with self.store.database.session() as session, session.begin():
            task = await lock_task(session, snapshot)
            if task.status is not TaskStatus.LEARNING:
                raise ValueError("发布报告必须在独立验证后进入 LEARNING")
            key: JsonObject = {"phase_version": task.status_version}
            ledger = LedgerService(session)
            previous = await cached(session, task.id, "release.report", key)
            if previous:
                return str(previous.id)
            records = await ledger.evidence_for_task(task.id)
            assessments = [e for e in records if e.source_tool == "release.assessment"]
            proofs = [e for e in records if e.source_tool == "verify_release"]
            if not assessments or not proofs:
                raise ValueError("发布报告缺少检查和独立验证")
            from app.verifier.releases import ReleaseVerification

            last = ReleaseVerification.model_validate_json(json.dumps(proofs[-1].result_snapshot))
            if not last.final or not last.passed:
                raise ValueError("未恢复的发布不能生成成功报告")
            report = {
                "task_id": str(task.id),
                "outcome": "rolled_back"
                if any(e.parameters.get("name") == "rollback_prod" for e in records)
                else "released",
                "checks": assessments[0].result_snapshot,
                "evidence_ids": [
                    str(e.id)
                    for e in records
                    if e.source_tool
                    in {
                        "release.assessment",
                        "release.review",
                        "action_plan",
                        "approval.request",
                        "approval.decision",
                        "execute_action",
                        "verify_release",
                    }
                ],
                "timeline": [
                    {
                        "evidence_id": str(e.id),
                        "source": e.source_tool,
                        "collected_at": e.collected_at.isoformat(),
                    }
                    for e in records
                    if e.source_tool
                    in {
                        "release.assessment",
                        "release.review",
                        "action_plan",
                        "approval.request",
                        "approval.decision",
                        "execute_action",
                        "verify_release",
                    }
                ],
            }
            evidence = await ledger.append_evidence(
                task_id=UUID(snapshot.task_id),
                source_tool="release.report",
                parameters=key,
                result_snapshot=json_object(report),
            )
            return str(evidence.id)
