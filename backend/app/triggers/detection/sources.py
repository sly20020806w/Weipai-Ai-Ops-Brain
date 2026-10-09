"""检测器只消费既有只读 Connector 的快照，不直接访问外部 API。"""

from datetime import datetime, timedelta

from app.connectors.kubernetes.client import KubernetesConnector
from app.connectors.observability.base import PrometheusConnector
from app.connectors.observability.models import MetricsQuery
from app.triggers.detection.config import DetectionConfig
from app.triggers.detection.detectors import detect_state, detect_trend
from app.triggers.detection.models import Observation


class DetectionSources:
    def __init__(self, kubernetes: KubernetesConnector, prometheus: PrometheusConnector) -> None:
        self.kubernetes, self.prometheus = kubernetes, prometheus

    async def collect(self, config: DetectionConfig, at: datetime) -> list[Observation]:
        config = DetectionConfig.model_validate(config)
        observations = []
        for rule in config.state_rules:
            deployments = await self.kubernetes.list_deployments(
                rule.namespace, service_name=rule.service_name
            )
            observation = detect_state(rule, deployments, self.kubernetes.cluster_name, at)
            if observation is not None:
                observations.append(observation)
        for trend in config.trend_rules:
            query = MetricsQuery(
                service_name=trend.service_name,
                metric_name=trend.metric_name,
                start=at - timedelta(seconds=config.lookback_seconds),
                end=at,
                step_seconds=config.step_seconds,
            )
            for series in await self.prometheus.query_metrics(query):
                observation = detect_trend(trend, series, query)
                if observation is not None:
                    observations.append(observation)
        return observations
