"""Postmortem Activity；Temporal 负责重试、恢复与改进任务派发。"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.db.session import Database
from app.learning.models import LearningRequest, LearningResult
from app.learning.service import LearningStore


class LearningActivities:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.store = LearningStore(database, settings)

    @activity.defn(name="learning.postmortem")
    async def generate(self, request: LearningRequest) -> LearningResult:
        try:
            return await self.store.generate(request)
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("复盘证据或事故状态被拒绝", non_retryable=True) from None
