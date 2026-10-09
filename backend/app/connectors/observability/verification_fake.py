"""Step 31 显式恢复快照；仅供 Fake 验收，不修改已有故障样例。"""

from datetime import datetime, timedelta

from app.connectors.observability.models import (
    LogRecord,
    MetricPoint,
    MetricSeries,
    Span,
    TraceRecord,
)


def verification_metrics(
    start: datetime, end: datetime, *, recovered: bool = True
) -> tuple[MetricSeries, ...]:
    count = int((end - start).total_seconds() // 60)
    return tuple(
        MetricSeries(
            service_name="payment-service",
            metric_name=metric,
            labels={"service": "payment-service"},
            points=tuple(
                MetricPoint(timestamp=start + timedelta(minutes=i), value=value)
                for i in range(count)
            ),
        )
        for metric, value in (
            ("http_5xx_ratio", 0.001 if recovered else 0.12),
            ("http_p99_ms", 120.0 if recovered else 1500.0),
            ("http_success_ratio", 0.999 if recovered else 0.88),
        )
    )


def verification_logs(start: datetime, *, recovered: bool = True) -> tuple[LogRecord, ...]:
    return (
        LogRecord(
            service_name="payment-service",
            timestamp=start + timedelta(minutes=1),
            level="INFO" if recovered else "ERROR",
            message="Fake 支付恢复观测",
            source_ref="sls:verification-fake",
        ),
    )


def verification_traces(start: datetime, *, recovered: bool = True) -> tuple[TraceRecord, ...]:
    timestamp = start + timedelta(minutes=1)
    return (
        TraceRecord(
            trace_id="verification-payment",
            service_name="payment-service",
            timestamp=timestamp,
            duration_ms=120.0 if recovered else 1500.0,
            source_ref="arms:verification-fake",
            spans=(
                Span(
                    trace_id="verification-payment",
                    span_id="payment",
                    service_name="payment-service",
                    timestamp=timestamp,
                    duration_ms=120.0 if recovered else 1500.0,
                    operation="POST /pay",
                    result_code="200" if recovered else "500",
                ),
            ),
        ),
    )
