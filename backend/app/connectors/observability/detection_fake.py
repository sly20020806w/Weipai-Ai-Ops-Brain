"""Step 23 可脚本化样例；仅测试/演示显式注入，不替换默认 Fake 数据。"""

from datetime import timedelta

from app.connectors.observability.fake import FakePrometheusConnector
from app.connectors.observability.models import MetricPoint, MetricSeries, MetricsQuery


class FakeDetectionPrometheusConnector(FakePrometheusConnector):
    def __init__(self, *, healthy: bool = False) -> None:
        super().__init__(())
        self.healthy = healthy

    async def query_metrics(self, query: MetricsQuery) -> tuple[MetricSeries, ...]:
        query = MetricsQuery.model_validate(query)
        self._check()
        values = {
            "disk_used_ratio": (0.5, 0.02),
            "http_requests_rate": (100.0, 10.0),
            "daily_cost": (100.0, 10.0),
            "cpu_usage_ratio": (0.4, 0.035),
        }
        if query.metric_name not in values:
            return ()
        base, growth = values[query.metric_name]
        count = int((query.end - query.start).total_seconds() / query.step_seconds)
        return (
            MetricSeries(
                service_name=query.service_name,
                metric_name=query.metric_name,
                labels={"service": query.service_name, "resource": "fake-resource"},
                points=tuple(
                    MetricPoint(
                        timestamp=query.start + timedelta(seconds=i * query.step_seconds),
                        value=base if self.healthy else base + growth * i,
                    )
                    for i in range(count)
                ),
            ),
        )
