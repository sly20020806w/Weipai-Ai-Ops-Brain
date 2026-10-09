"""三个同构的只读接口；查询不包含端点或身份参数。"""

from abc import abstractmethod

from app.connectors.base import ReadOnlyConnector
from app.connectors.observability.models import (
    LogRecord,
    MetricSeries,
    MetricsQuery,
    TraceRecord,
    Window,
)


class ObservabilityError(RuntimeError):
    pass


class ObservabilityResponseError(ObservabilityError):
    pass


class ObservabilityHTTPError(ObservabilityError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"可观测性读取失败（HTTP {status_code}）")


class ObservabilityTimeout(ObservabilityError):
    pass


class PrometheusConnector(ReadOnlyConnector):
    @abstractmethod
    async def query_metrics(self, query: MetricsQuery) -> tuple[MetricSeries, ...]: ...


class SLSConnector(ReadOnlyConnector):
    @abstractmethod
    async def query_logs(self, query: Window) -> tuple[LogRecord, ...]: ...


class ARMSConnector(ReadOnlyConnector):
    @abstractmethod
    async def query_traces(self, query: Window) -> tuple[TraceRecord, ...]: ...
