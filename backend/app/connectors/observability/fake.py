"""可注入的离线快照；默认数据包括边界外和其他服务记录以验证筛选。"""

from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter

from app.connectors.base import ReadOnlyConnector
from app.connectors.observability.base import (
    ARMSConnector,
    ObservabilityError,
    PrometheusConnector,
    SLSConnector,
)
from app.connectors.observability.models import (
    LogRecord,
    MetricPoint,
    MetricSeries,
    MetricsQuery,
    Span,
    TraceRecord,
    Window,
)

SAMPLE_START = datetime(2026, 10, 1, 1, tzinfo=UTC)
SAMPLE_END = SAMPLE_START + timedelta(minutes=10)


def discovery_traces(end: datetime) -> tuple[TraceRecord, ...]:
    """Step 18 专用样例：时间相对查询窗口移动，显式包含上下游父子 Span。"""
    timestamp = end - timedelta(seconds=30)
    trace_id = "discovery-payment-sample"
    return (
        TraceRecord(
            trace_id=trace_id,
            service_name="payment-service",
            timestamp=timestamp,
            duration_ms=1500.0,
            source_ref="arms:discovery-sample",
            spans=tuple(
                Span(
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent,
                    service_name=service,
                    timestamp=timestamp,
                    duration_ms=1000.0,
                    operation="Discovery 样例调用",
                    result_code="ok",
                )
                for span_id, parent, service in (
                    ("checkout", None, "checkout-service"),
                    ("payment", "checkout", "payment-service"),
                    ("db", "payment", "payment-db"),
                )
            ),
        ),
    )


def sample_metrics() -> tuple[MetricSeries, ...]:
    return tuple(
        MetricSeries(
            service_name=service,
            metric_name="http_5xx_ratio",
            labels={"__name__": "http_5xx_ratio", "service": service},
            points=tuple(
                MetricPoint(timestamp=SAMPLE_START + timedelta(minutes=minute), value=value)
                for minute, value in [(-1, 0.01), (0, 0.02), (5, 0.12), (10, 0.03), (11, 0.01)]
            ),
        )
        for service in ("payment-service", "checkout-service")
    )


def sample_logs() -> tuple[LogRecord, ...]:
    return tuple(
        LogRecord(
            service_name=service,
            timestamp=SAMPLE_START + timedelta(minutes=minute),
            level="ERROR",
            message="样例：数据库连接池等待超时",
            source_ref="sls:fake/payment-logs",
        )
        for service in ("payment-service", "checkout-service")
        for minute in (-1, 0, 5, 10, 11)
    )


def sample_traces() -> tuple[TraceRecord, ...]:
    traces: list[TraceRecord] = []
    for service in ("payment-service", "checkout-service"):
        for minute in (-1, 0, 5, 10, 11):
            timestamp = SAMPLE_START + timedelta(minutes=minute)
            trace_id = f"trace-{service}-{minute}"
            root = Span(
                trace_id=trace_id,
                span_id="root",
                service_name=service,
                timestamp=timestamp,
                duration_ms=1500.0,
                operation="POST /pay",
                result_code="500",
            )
            child = Span(
                trace_id=trace_id,
                span_id="db",
                parent_span_id="root",
                service_name="payment-db",
                timestamp=timestamp + timedelta(seconds=1),
                duration_ms=1400.0,
                operation="获取数据库连接",
                result_code="timeout",
            )
            traces.append(
                TraceRecord(
                    trace_id=trace_id,
                    service_name=service,
                    timestamp=timestamp,
                    duration_ms=1500.0,
                    spans=(root, child),
                    source_ref=f"arms:{trace_id}",
                )
            )
    return tuple(traces)


class _FakeReader(ReadOnlyConnector):
    def __init__(self) -> None:
        super().__init__()
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True

    def _check(self) -> None:
        if self._closed:
            raise ObservabilityError("可观测性 Connector 已关闭")


class FakePrometheusConnector(_FakeReader, PrometheusConnector):
    def __init__(self, series: tuple[MetricSeries, ...] | None = None) -> None:
        super().__init__()
        self._series = tuple(
            item.model_copy(deep=True)
            for item in TypeAdapter(tuple[MetricSeries, ...]).validate_python(
                series if series is not None else sample_metrics(), strict=True
            )
        )

    async def query_metrics(self, query: MetricsQuery) -> tuple[MetricSeries, ...]:
        query = MetricsQuery.model_validate(query)
        self._check()
        result: list[MetricSeries] = []
        for series in self._series:
            if series.service_name != query.service_name or series.metric_name != query.metric_name:
                continue
            # Fake 只保留查询采样网格上的已注入点，不合成不存在的观测。
            points = tuple(
                sorted(
                    (
                        point
                        for point in series.points
                        if query.contains(point.timestamp)
                        and (point.timestamp - query.start).total_seconds() % query.step_seconds
                        == 0
                    ),
                    key=lambda point: point.timestamp,
                )
            )
            if points:
                result.append(
                    MetricSeries(
                        service_name=series.service_name,
                        metric_name=series.metric_name,
                        labels=dict(series.labels),
                        points=points,
                    ).model_copy(deep=True)
                )
        return tuple(result)


class FakeSLSConnector(_FakeReader, SLSConnector):
    def __init__(self, logs: tuple[LogRecord, ...] | None = None) -> None:
        super().__init__()
        self._logs = tuple(
            item.model_copy(deep=True)
            for item in TypeAdapter(tuple[LogRecord, ...]).validate_python(
                logs if logs is not None else sample_logs(), strict=True
            )
        )

    async def query_logs(self, query: Window) -> tuple[LogRecord, ...]:
        query = Window.model_validate(query)
        self._check()
        return tuple(
            item.model_copy(deep=True)
            for item in sorted(
                (
                    log
                    for log in self._logs
                    if log.service_name == query.service_name and query.contains(log.timestamp)
                ),
                key=lambda log: log.timestamp,
            )
        )


class FakeARMSConnector(_FakeReader, ARMSConnector):
    def __init__(self, traces: tuple[TraceRecord, ...] | None = None) -> None:
        super().__init__()
        self._traces = tuple(
            item.model_copy(deep=True)
            for item in TypeAdapter(tuple[TraceRecord, ...]).validate_python(
                traces if traces is not None else sample_traces(), strict=True
            )
        )

    async def query_traces(self, query: Window) -> tuple[TraceRecord, ...]:
        query = Window.model_validate(query)
        self._check()
        return tuple(
            TraceRecord(
                trace_id=trace.trace_id,
                service_name=trace.service_name,
                timestamp=trace.timestamp,
                duration_ms=trace.duration_ms,
                source_ref=trace.source_ref,
                spans=tuple(
                    span.model_copy(deep=True)
                    for span in trace.spans
                    if query.contains(span.timestamp)
                ),
            )
            for trace in sorted(self._traces, key=lambda t: (t.timestamp, t.trace_id))
            if trace.service_name == query.service_name and query.contains(trace.timestamp)
        )
