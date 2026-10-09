"""I/O 仅在 Activity；六类采集全部成功后原子提交，错误进入历史前脱敏。"""

from contextlib import AsyncExitStack
from datetime import datetime

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.changes.models import DeploymentQuery
from app.db.session import Database
from app.graph.changes.schemas import TimelineRequest, TimelineResult
from app.graph.changes.service import ChangeConflict, TimelineService
from app.graph.changes.sources import configured_sources


class TimelineActivities:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    @activity.defn(name="timeline.collect")
    async def collect(self, request: TimelineRequest) -> TimelineResult:
        try:
            query = DeploymentQuery(
                service_name=request.service_name,
                start=datetime.fromisoformat(request.start),
                end=datetime.fromisoformat(request.end),
            )
        except (ValueError, TypeError):
            raise ApplicationError("Change Timeline 输入无效", non_retryable=True) from None
        try:
            async with AsyncExitStack() as stack:
                sources = await configured_sources(stack, self.settings)
                snapshot = await sources.collect(query)
            async with self.database.session() as session, session.begin():
                return await TimelineService(session).persist(snapshot)
        except ChangeConflict:
            raise ApplicationError("Change Timeline 源证据冲突", non_retryable=True) from None
        except Exception:
            raise ApplicationError("Change Timeline 采集或提交失败") from None
