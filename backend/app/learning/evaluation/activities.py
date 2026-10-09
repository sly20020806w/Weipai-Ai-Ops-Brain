"""Replay Activity 的持久化检查点复用已提交响应与观察。"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.db.session import Database
from app.learning.evaluation.models import ReplayRequest
from app.learning.evaluation.replay import ReplayStore


class ReplayActivities:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.store = ReplayStore(database, settings)

    @activity.defn(name="learning.replay")
    async def replay(self, request_json: str) -> str:
        try:
            result = await self.store.run(ReplayRequest.model_validate_json(request_json))
            return result.model_dump_json()
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("历史事故回放输入被拒绝", non_retryable=True) from None
