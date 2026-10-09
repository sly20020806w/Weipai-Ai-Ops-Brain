"""I/O 与事务在 Activity；定时、等待、重试全部交由 Temporal。"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.kubernetes.execution import (
    FakeKubernetesWriteConnector,
    KubernetesWriteConnector,
)
from app.connectors.war_room.facts import FakeWarRoomConnector, WarRoomConnector
from app.db.session import Database
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.planning.models import PlanningResult
from app.tasks.war_room.models import WarRoomRequest, WarRoomResult, WarRoomVerifyRequest
from app.tasks.war_room.service import WarRoomService
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.war_room import war_room_registry


class WarRoomActivities:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        facts: WarRoomConnector | None = None,
        resources: KubernetesWriteConnector | None = None,
    ) -> None:
        if settings.app_env not in {"local", "test"} or settings.connector_mode.value != "fake":
            raise ValueError("重大保障首批仅允许本机 Fake")
        self.database, self.settings = database, settings
        self.facts = facts or FakeWarRoomConnector()
        self.resources = resources or FakeKubernetesWriteConnector()

    def service(self, session: object) -> WarRoomService:
        from sqlalchemy.ext.asyncio import AsyncSession

        if not isinstance(session, AsyncSession):
            raise TypeError("无效数据库会话")
        return WarRoomService(
            session,
            self.settings,
            ToolDispatcher(
                war_room_registry(session, self.settings, self.facts, self.resources),
                create_policy_engine(self.settings),
                LedgerService(session),
            ),
        )

    @activity.defn(name="war_room.input")
    async def input(self, task_id: str) -> str:
        from uuid import UUID

        from app.tasks.models import AITask
        from app.tasks.war_room.service import submission

        async with self.database.session() as session:
            task = await session.get(AITask, UUID(task_id))
            if task is None:
                raise ValueError("保障任务不存在")
            return (await submission(session, task)).model_dump_json()

    @activity.defn(name="war_room.interval")
    async def interval(self, task_id: str) -> float:
        await self.input(task_id)
        return self.settings.war_room_config.interval_seconds

    @activity.defn(name="war_room.assess")
    async def assess(self, request: WarRoomRequest) -> WarRoomResult:
        try:
            async with self.database.session() as session, session.begin():
                return await self.service(session).assess(request)
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("重大保障输入、阶段或证据无效", non_retryable=True) from None

    @activity.defn(name="war_room.review")
    async def review(self, request: WarRoomVerifyRequest) -> str:
        async with self.database.session() as session, session.begin():
            return await self.service(session).review(request)

    @activity.defn(name="war_room.plan")
    async def plan(self, request: WarRoomVerifyRequest) -> PlanningResult:
        async with self.database.session() as session, session.begin():
            return await self.service(session).plan(request)

    @activity.defn(name="war_room.report")
    async def report(self, request: TaskSnapshot) -> str:
        async with self.database.session() as session, session.begin():
            return await self.service(session).report(request)
