"""Reviewer 专用离线事实：显式观测网络、缓存和第三方，避免把缺失当正常。"""

from typing import Literal

from app.connectors.observability.fake import sample_traces
from app.connectors.observability.models import Span, TraceRecord


def reviewer_traces(mode: Literal["clear", "contradicted"] = "clear") -> tuple[TraceRecord, ...]:
    records = []
    for trace in sample_traces():
        spans = [*trace.spans]
        if mode == "contradicted":
            spans[1] = spans[1].model_copy(update={"result_code": "ok", "duration_ms": 10.0})
        for span_id, service, operation in (
            ("network", trace.service_name, "TCP connect"),
            ("redis", "payment-cache", "Redis GET"),
            ("third_party", "payment-provider", "第三方支付请求"),
        ):
            spans.append(
                Span(
                    trace_id=trace.trace_id,
                    span_id=span_id,
                    parent_span_id="root",
                    service_name=service,
                    timestamp=trace.timestamp,
                    duration_ms=10.0,
                    operation=operation,
                    result_code="timeout"
                    if mode == "contradicted" and span_id == "network"
                    else "ok",
                )
            )
        records.append(trace.model_copy(update={"spans": tuple(spans)}))
    return tuple(records)
