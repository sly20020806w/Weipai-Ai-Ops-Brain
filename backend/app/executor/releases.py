"""发布 Executor 复用既有精确授权与幂等动作端，并在签发前重读发布材料。"""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.changes.releases import FakeReleaseState
from app.db.session import Database
from app.executor.models import ExecutionRequest
from app.executor.service import ExecutionStore
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.releases.models import ReleaseAssessment, ReleaseManifest, manifest_hash
from app.tasks.releases.service import ReleaseStore


class ReleaseExecutor(ExecutionStore):
    def __init__(self, database: Database, settings: Settings, state: FakeReleaseState) -> None:
        super().__init__(database, settings, state.writer)
        self.releases = ReleaseStore(database, settings, state)

    async def authorize(
        self, session: AsyncSession, request: ExecutionRequest
    ) -> tuple[AITask, ActionPlan]:
        task, plan = await super().authorize(session, request)
        record = await LedgerService(session).get_evidence(plan.conclusion_evidence_id)
        assessment = ReleaseAssessment.model_validate_json(json.dumps(record.result_snapshot))
        self.releases.settings = self.settings
        current = await self.releases.read(
            session,
            task.id,
            "get_release_request",
            {"release_id": assessment.manifest.release_id},
            "executor",
        )
        manifest = ReleaseManifest.model_validate_json(json.dumps(current.result_snapshot))
        if manifest_hash(manifest) != assessment.manifest_hash:
            raise PermissionError("发布材料已变化，旧审批与执行意图失效")
        return task, plan
