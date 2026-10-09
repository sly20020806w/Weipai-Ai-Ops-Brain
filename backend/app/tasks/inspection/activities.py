"""业务事务在 Activity；重试、定时与派发均交给 Temporal。"""

from collections.abc import Callable
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.feishu.base import FeishuConnector
from app.connectors.feishu.factory import create_feishu_connector
from app.connectors.inspection.client import InspectionConnector
from app.connectors.inspection.factory import create_inspection_connector
from app.db.session import Database
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.embedding import embedding_client
from app.runbooks.service import RunbookService
from app.tasks.inspection.models import InspectionRequest, InspectionResult
from app.tasks.inspection.service import InspectionService, notify_risk
from app.tools.dispatcher import ToolDispatcher
from app.tools.inspection import register_inspection_tools
from app.tools.registry import ToolRegistry
from app.tools.runbooks import register_runbook_tools


class InspectionActivities:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        connector_factory: Callable[[], InspectionConnector] | None = None,
        feishu: FeishuConnector | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.connector_factory = connector_factory or (
            lambda: create_inspection_connector(settings)
        )
        self.feishu = feishu

    @activity.defn(name="inspection.scan")
    async def scan(self, request: InspectionRequest) -> InspectionResult:
        try:
            async with self.database.session() as session, session.begin():
                async with self.connector_factory() as connector:
                    registry = ToolRegistry()
                    register_inspection_tools(registry, connector)
                    register_runbook_tools(
                        registry,
                        RunbookService(
                            session, lambda value: embedding_client(self.settings, value)
                        ),
                    )
                    return await InspectionService(
                        session,
                        self.settings,
                        ToolDispatcher(
                            registry, create_policy_engine(self.settings), LedgerService(session)
                        ),
                    ).scan(request)
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("巡检范围、规则或任务不匹配", non_retryable=True) from None

    @activity.defn(name="inspection.notify")
    async def notify(self, risk_id: str) -> None:
        if self.feishu is not None:
            async with self.database.session() as session, session.begin():
                await notify_risk(session, UUID(risk_id), self.feishu)
        else:
            async with create_feishu_connector(self.settings) as connector:
                async with self.database.session() as session, session.begin():
                    await notify_risk(session, UUID(risk_id), connector)
