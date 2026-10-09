"""采集与原子入库在 Activity；失败由 Temporal 有限重试。"""

from contextlib import AsyncExitStack
from datetime import datetime

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.kubernetes.factory import create_kubernetes_connector
from app.connectors.observability.factory import create_prometheus_connector
from app.db.base import UTCDateTime
from app.db.session import Database
from app.triggers.detection.models import (
    DetectionBatch,
    DetectionRequest,
    DetectionResult,
    Observation,
)
from app.triggers.detection.service import DetectionService
from app.triggers.detection.sources import DetectionSources


class DetectionActivities:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    @activity.defn(name="detection.collect")
    async def collect(self, value: DetectionRequest) -> DetectionBatch:
        try:
            at = UTCDateTime.normalize(datetime.fromisoformat(value.observed_at))
            if at is None:
                raise ValueError
        except (ValueError, TypeError):
            raise ApplicationError("检测采集时间无效", non_retryable=True) from None
        try:
            async with AsyncExitStack() as stack:
                kubernetes = await stack.enter_async_context(
                    create_kubernetes_connector(self.settings)
                )
                prometheus = await stack.enter_async_context(
                    create_prometheus_connector(self.settings)
                )
                values = await DetectionSources(kubernetes, prometheus).collect(
                    self.settings.detection_config, at
                )
            return DetectionBatch(at.isoformat(), [value.model_dump_json() for value in values])
        except Exception:
            raise ApplicationError("状态或趋势采集失败，请检查检测配置及只读来源") from None

    @activity.defn(name="detection.persist")
    async def persist(self, value: DetectionBatch) -> DetectionResult:
        try:
            at = UTCDateTime.normalize(datetime.fromisoformat(value.observed_at))
            if at is None or len(value.observations) > 10000:
                raise ValueError
            values = [Observation.model_validate_json(item) for item in value.observations]
        except (ValueError, TypeError):
            raise ApplicationError("检测入库参数无效", non_retryable=True) from None
        try:
            async with self.database.session() as session, session.begin():
                return await DetectionService(session).persist(values, at.isoformat())
        except Exception:
            raise ApplicationError("检测事件、证据或游标写入失败") from None

    async def evaluate(self, value: DetectionRequest) -> DetectionResult:
        return await self.persist(await self.collect(value))
