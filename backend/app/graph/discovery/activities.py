"""I/O 全在 Activity 中执行；不将 Connector 或数据库会话传入 Workflow。"""

from contextlib import AsyncExitStack
from datetime import datetime

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.db.base import UTCDateTime
from app.db.session import Database
from app.graph.discovery.models import DiscoveryRequest, DiscoveryResult
from app.graph.discovery.service import persist_snapshot
from app.graph.discovery.sources import configured_sources
from app.graph.service import GraphService


class DiscoveryActivities:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    @activity.defn(name="discovery.refresh")
    async def refresh(self, request: DiscoveryRequest) -> DiscoveryResult:
        try:
            end = UTCDateTime.normalize(datetime.fromisoformat(request.observed_at))
            if (
                end is None
                or type(request.lookback_seconds) is not int
                or not (1 <= request.lookback_seconds <= 86400)
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise ApplicationError("Discovery 输入无效", non_retryable=True) from None
        try:
            async with AsyncExitStack() as stack:
                sources = await configured_sources(stack, self.settings, end)
                snapshot = await sources.collect(end, request.lookback_seconds)
            async with self.database.session() as session, session.begin():
                return await persist_snapshot(GraphService(session), snapshot, end.isoformat())
        except Exception:
            # 仅固定错误消息进入 Temporal 历史，防止源系统响应/身份进入持久化。
            raise ApplicationError("Discovery 采集或图刷新失败") from None
