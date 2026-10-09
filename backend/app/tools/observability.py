"""三个 L0 Tool 仅声明高级查询，实际调用统一经 Dispatcher 留证据与审计。"""

from typing import Literal

from app.connectors.observability.base import ARMSConnector, PrometheusConnector, SLSConnector
from app.connectors.observability.models import (
    LogRecord,
    MetricSeries,
    MetricsQuery,
    TopologyEdge,
    TraceRecord,
    Window,
    topology,
)
from app.policy.models import RiskLevel
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class MetricsInput(MetricsQuery, ToolModel):
    pass


class WindowInput(Window, ToolModel):
    pass


class MetricsOutput(Window, ToolModel):
    source: Literal["prometheus"] = "prometheus"
    series: tuple[MetricSeries, ...]


class LogsOutput(Window, ToolModel):
    source: Literal["sls"] = "sls"
    logs: tuple[LogRecord, ...]


class TracesOutput(Window, ToolModel):
    source: Literal["arms"] = "arms"
    traces: tuple[TraceRecord, ...]
    topology: tuple[TopologyEdge, ...]


def register_observability_tools(
    registry: ToolRegistry, prometheus: PrometheusConnector, sls: SLSConnector, arms: ARMSConnector
) -> None:
    async def metrics(query: MetricsInput) -> MetricsOutput:
        return MetricsOutput(
            service_name=query.service_name,
            start=query.start,
            end=query.end,
            series=await prometheus.query_metrics(MetricsQuery.model_validate(query.model_dump())),
        )

    async def logs(query: WindowInput) -> LogsOutput:
        return LogsOutput(
            service_name=query.service_name,
            start=query.start,
            end=query.end,
            logs=await sls.query_logs(Window.model_validate(query.model_dump())),
        )

    async def traces(query: WindowInput) -> TracesOutput:
        records = await arms.query_traces(Window.model_validate(query.model_dump()))
        return TracesOutput(
            service_name=query.service_name,
            start=query.start,
            end=query.end,
            traces=records,
            topology=topology(records),
        )

    registry.register(
        name="query_metrics",
        description="按服务与 UTC 时间窗读取 Prometheus 指标",
        input_model=MetricsInput,
        output_model=MetricsOutput,
        handler=metrics,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="query_logs",
        description="按服务与 UTC 时间窗读取 SLS 日志",
        input_model=WindowInput,
        output_model=LogsOutput,
        handler=logs,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="query_traces",
        description="按服务与 UTC 时间窗读取 ARMS Trace 与调用拓扑",
        input_model=WindowInput,
        output_model=TracesOutput,
        handler=traces,
        risk_level=RiskLevel.L0,
    )
