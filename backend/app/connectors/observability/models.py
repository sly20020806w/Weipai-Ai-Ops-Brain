"""按需查询的最小快照，统一使用 UTC 半开时间窗 [start, end)。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ServiceName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
MetricName = Annotated[str, Field(pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,127}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]


class Snapshot(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class Window(Snapshot):
    service_name: ServiceName
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self) -> "Window":
        if not timedelta(0) < self.end - self.start <= timedelta(days=1):
            raise ValueError("时间窗必须 start < end 且不超过 24 小时")
        return self

    def contains(self, timestamp: datetime) -> bool:
        return self.start <= timestamp < self.end


class MetricsQuery(Window):
    metric_name: MetricName = "http_5xx_ratio"
    step_seconds: int = Field(default=60, ge=1, le=3600)

    @model_validator(mode="after")
    def bounded_samples(self) -> "MetricsQuery":
        if (self.end - self.start).total_seconds() / self.step_seconds > 11000:
            raise ValueError("单条序列的采样点不能超过 11000")
        return self


class MetricPoint(Snapshot):
    timestamp: AwareDatetime
    value: float = Field(allow_inf_nan=False)

    @field_validator("timestamp")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class MetricSeries(Snapshot):
    service_name: ServiceName
    metric_name: MetricName
    labels: dict[str, str]
    points: tuple[MetricPoint, ...]


class LogRecord(Snapshot):
    service_name: ServiceName
    timestamp: AwareDatetime
    level: str = Field(min_length=1, max_length=32)
    message: str = Field(max_length=100_000)
    source_ref: str = Field(min_length=1)

    @field_validator("timestamp")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class Span(Snapshot):
    trace_id: Identifier
    span_id: Identifier
    parent_span_id: str | None = Field(default=None, max_length=256)
    service_name: ServiceName
    timestamp: AwareDatetime
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    operation: str = Field(min_length=1, max_length=2048)
    result_code: str = Field(max_length=64)

    @field_validator("timestamp")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class TraceRecord(Snapshot):
    trace_id: Identifier
    service_name: ServiceName
    timestamp: AwareDatetime
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    spans: tuple[Span, ...]
    source_ref: str = Field(min_length=1)

    @field_validator("timestamp")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def span_ids(self) -> "TraceRecord":
        if len({span.span_id for span in self.spans}) != len(self.spans):
            raise ValueError("同一 Trace 中 Span ID 不能重复")
        if any(span.trace_id != self.trace_id for span in self.spans):
            raise ValueError("Span 必须属于当前 Trace")
        return self


class TopologyEdge(Snapshot):
    trace_id: Identifier
    parent_span_id: Identifier
    span_id: Identifier
    source_service: ServiceName
    target_service: ServiceName
    source: Literal["arms"] = "arms"


def topology(traces: tuple[TraceRecord, ...]) -> tuple[TopologyEdge, ...]:
    """仅从返回的 Span 父子关系取样，不推断缺失关系或更新 Context Graph。"""
    edges: list[TopologyEdge] = []
    for trace in traces:
        by_id = {span.span_id: span for span in trace.spans}
        for span in trace.spans:
            parent = by_id.get(span.parent_span_id or "")
            if parent is not None and parent.span_id != span.span_id:
                edges.append(
                    TopologyEdge(
                        trace_id=trace.trace_id,
                        parent_span_id=parent.span_id,
                        span_id=span.span_id,
                        source_service=parent.service_name,
                        target_service=span.service_name,
                    )
                )
    return tuple(edges)
