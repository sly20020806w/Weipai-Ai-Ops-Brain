"""Prometheus query_range：仅允许服务限定的单指标查询。"""

from datetime import UTC, datetime
from typing import Literal

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.connectors.models import ReaderCredentials
from app.connectors.observability.base import ObservabilityResponseError, PrometheusConnector
from app.connectors.observability.config import PrometheusConfig
from app.connectors.observability.http import ReaderHTTP
from app.connectors.observability.models import MetricPoint, MetricSeries, MetricsQuery


class _Series(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    metric: dict[str, str]
    values: tuple[tuple[float, str], ...]


class _Matrix(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    resultType: Literal["matrix"]
    result: tuple[_Series, ...]


class _Response(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    status: Literal["success"]
    data: _Matrix
    warnings: tuple[str, ...] = ()
    infos: tuple[str, ...] = ()


class HTTPPrometheusConnector(PrometheusConnector):
    def __init__(
        self,
        config: PrometheusConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        assert self.reader_credentials is not None
        if self.reader_credentials.connector != "prometheus":
            raise ValueError("Prometheus 只接受 prometheus Reader 凭证")
        token = self.reader_credentials.token.get_secret_value()
        if any(char.isspace() or ord(char) < 32 for char in token):
            raise ValueError("Prometheus Reader token 不能包含空白或控制字符")
        self._config = PrometheusConfig.model_validate(config)
        self._http = ReaderHTTP(self._config, transport)
        self._headers = {"Authorization": f"Bearer {token}"}

    async def aclose(self) -> None:
        await self._http.client.aclose()

    async def query_metrics(self, query: MetricsQuery) -> tuple[MetricSeries, ...]:
        query = MetricsQuery.model_validate(query)
        response = await self._http.read(
            "api/v1/query_range",
            {
                "query": (
                    f'{query.metric_name}{{{self._config.service_label}="{query.service_name}"}}'
                ),
                "start": query.start.isoformat(),
                "end": query.end.isoformat(),
                "step": str(query.step_seconds),
                "limit": str(self._config.max_series + 1),
            },
            self._headers,
        )
        try:
            data = _Response.model_validate_json(response.content)
            if data.warnings or data.infos or len(data.data.result) > self._config.max_series:
                raise ValueError("查询有警告或超出序列上限")
            result: list[MetricSeries] = []
            for series in data.data.result:
                if series.metric.get(self._config.service_label) != query.service_name:
                    raise ValueError("返回了其他服务的序列")
                if series.metric.get("__name__", query.metric_name) != query.metric_name:
                    raise ValueError("返回了其他指标")
                points = tuple(
                    MetricPoint(
                        timestamp=datetime.fromtimestamp(timestamp, UTC),
                        value=float(value),
                    )
                    for timestamp, value in series.values
                )
                if len({point.timestamp for point in points}) != len(points):
                    raise ValueError("序列存在重复采样时间")
                points = tuple(
                    sorted(
                        (p for p in points if query.contains(p.timestamp)),
                        key=lambda p: p.timestamp,
                    )
                )
                if points:
                    result.append(
                        MetricSeries(
                            service_name=query.service_name,
                            metric_name=query.metric_name,
                            labels=series.metric,
                            points=points,
                        )
                    )
            return tuple(result)
        except (ValidationError, ValueError, OverflowError, OSError):
            raise ObservabilityResponseError("Prometheus 响应不符合完整的指标协议") from None
