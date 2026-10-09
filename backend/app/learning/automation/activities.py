"""本库扫描 Activity；失败与任务派发由 Temporal 重试。"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.db.session import Database
from app.learning.automation.models import AutomationResult, ScanRequest
from app.learning.automation.service import AutomationService


class AutomationActivities:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    @activity.defn(name="automation.scan")
    async def scan(self, request: ScanRequest) -> AutomationResult:
        try:
            async with self.database.session() as session, session.begin():
                return await AutomationService(session).scan(
                    self.settings.automation_config, request.end
                )
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("自动化统计来源或扫描范围无效", non_retryable=True) from None
