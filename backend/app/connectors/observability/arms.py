"""ARMS SearchTracesByPage + GetTrace，拓扑从实际返回的 Span 父子关系产生。"""

import json
import math
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.connectors.models import ReaderCredentials
from app.connectors.observability.base import ARMSConnector, ObservabilityResponseError
from app.connectors.observability.config import ARMSConfig
from app.connectors.observability.http import (
    ReaderHTTP,
    arms_headers,
    json_response,
    object_response,
    reader_key,
)
from app.connectors.observability.models import Span, TraceRecord, Window


class _Summary(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    TraceID: str = Field(min_length=1)
    ServiceName: str
    Timestamp: int
    Duration: float = Field(ge=0, allow_inf_nan=False)


class _Page(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    PageNumber: int = Field(ge=1)
    PageSize: int = Field(ge=1)
    Total: int = Field(ge=0)
    TraceInfos: tuple[_Summary, ...]


class HTTPARMSConnector(ARMSConnector):
    def __init__(
        self,
        config: ARMSConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        assert self.reader_credentials is not None
        if self.reader_credentials.connector != "arms":
            raise ValueError("ARMS 只接受 arms Reader 凭证")
        self._config = ARMSConfig.model_validate(config)
        self._key = reader_key(self.reader_credentials)
        self._http = ReaderHTTP(self._config, transport)

    async def aclose(self) -> None:
        await self._http.client.aclose()

    async def _call(self, action: str, params: dict[str, str]) -> dict[str, object]:
        params = {"RegionId": self._config.region_id, **params}
        host = urlsplit(self._config.base_url).netloc
        response = await self._http.read("/", params, arms_headers(self._key, host, action, params))
        data = object_response(json_response(response))
        if "Code" in data:
            raise ObservabilityResponseError("ARMS 返回业务错误")
        return data

    async def _spans(self, trace_id: str, params: dict[str, str]) -> tuple[Span, ...]:
        spans: dict[str, Span] = {}
        for page in range(1, self._config.max_pages + 1):
            data = await self._call(
                "GetTrace",
                {
                    **params,
                    "TraceID": trace_id,
                    "PageNumber": str(page),
                    "PageSize": str(self._config.page_size),
                },
            )
            raw = data.get("Spans")
            # Application Monitoring 返回数组，XTrace 返回 {Span: [...]}。
            if isinstance(raw, dict):
                raw = raw.get("Span")
            if not isinstance(raw, list) or len(raw) > self._config.page_size:
                raise ObservabilityResponseError("ARMS Span 列表协议不符")

            def collect(
                values: list[object],
                depth: int = 0,
                previous: frozenset[str] = frozenset(spans),
            ) -> None:
                if depth > 64:
                    raise ValueError("Span 嵌套超过上限")
                for value in values:
                    if isinstance(value, list):
                        collect(value, depth + 1)
                        continue
                    item = object_response(value)
                    unit = 1000 if self._config.span_timestamp_unit == "milliseconds" else 1000000
                    timestamp = item["Timestamp"]
                    duration = item["Duration"]
                    if type(timestamp) is not int or type(duration) not in {float, int}:
                        raise ValueError("Span 时间/耗时类型无效")
                    span = Span.model_validate(
                        {
                            "trace_id": item["TraceID"],
                            "span_id": item["SpanId"],
                            "parent_span_id": item.get("ParentSpanId") or None,
                            "service_name": item["ServiceName"],
                            "timestamp": datetime.fromtimestamp(timestamp / unit, UTC),
                            "duration_ms": duration,
                            "operation": item["OperationName"],
                            "result_code": item["ResultCode"],
                        }
                    )
                    if span.trace_id != trace_id or span.span_id in previous:
                        raise ValueError("Span Trace ID 错配或跨页重复")
                    existing = spans.get(span.span_id)
                    if existing is not None and existing != span:
                        raise ValueError("Span 内容冲突")
                    spans[span.span_id] = span
                    children = item.get("Children", [])
                    if not isinstance(children, list):
                        raise ValueError("Span Children 不是数组")
                    collect(children, depth + 1)

            try:
                collect(raw)
            except (ValueError, ValidationError, KeyError, OverflowError, OSError):
                raise ObservabilityResponseError("ARMS Span 响应协议不符") from None
            if len(raw) < self._config.page_size:
                return tuple(spans.values())
        raise ObservabilityResponseError("ARMS Span 超出分页上限，结果不完整")

    async def query_traces(self, query: Window) -> tuple[TraceRecord, ...]:
        query = Window.model_validate(query)
        # 以整数毫秒覆盖请求时间窗，再在本地按精确 UTC 边界裁剪。
        params = {
            "StartTime": str(math.floor(query.start.timestamp() * 1000)),
            "EndTime": str(math.ceil(query.end.timestamp() * 1000)),
        }
        summaries: list[_Summary] = []
        total: int | None = None
        seen: set[str] = set()
        for index in range(1, self._config.max_pages + 1):
            data = await self._call(
                "SearchTracesByPage",
                {
                    **params,
                    "ServiceName": query.service_name,
                    "PageNumber": str(index),
                    "PageSize": str(self._config.page_size),
                    "Reverse": "false",
                },
            )
            try:
                page = _Page.model_validate_json(json.dumps(data["PageBean"]))
                if (
                    page.PageNumber != index
                    or page.PageSize != self._config.page_size
                    or len(page.TraceInfos) > page.PageSize
                ):
                    raise ValueError("Trace 页号/数量不一致")
                if total is None:
                    total = page.Total
                elif total != page.Total:
                    raise ValueError("Trace 查询分页期间总数变化")
                for summary in page.TraceInfos:
                    if summary.ServiceName != query.service_name or summary.TraceID in seen:
                        raise ValueError("Trace 服务不符或 ID 重复")
                    seen.add(summary.TraceID)
                    summaries.append(summary)
                if len(summaries) > total or (not page.TraceInfos and len(summaries) < total):
                    raise ValueError("Trace 分页不完整")
            except (ValueError, ValidationError, KeyError):
                raise ObservabilityResponseError("ARMS Trace 响应协议不符") from None
            if len(summaries) == total:
                break
        else:
            raise ObservabilityResponseError("ARMS Trace 超出分页上限，结果不完整")
        result: list[TraceRecord] = []
        for summary in summaries:
            try:
                timestamp = datetime.fromtimestamp(summary.Timestamp / 1000, UTC)
            except (ValueError, OverflowError, OSError):
                raise ObservabilityResponseError("ARMS Trace 时间无效") from None
            if not query.contains(timestamp):
                continue
            spans = await self._spans(summary.TraceID, params)
            result.append(
                TraceRecord(
                    trace_id=summary.TraceID,
                    service_name=query.service_name,
                    timestamp=timestamp,
                    duration_ms=summary.Duration,
                    source_ref=f"arms:{summary.TraceID}",
                    spans=tuple(
                        sorted(
                            (s for s in spans if query.contains(s.timestamp)),
                            key=lambda s: (s.timestamp, s.span_id),
                        )
                    ),
                )
            )
        return tuple(sorted(result, key=lambda trace: (trace.timestamp, trace.trace_id)))
