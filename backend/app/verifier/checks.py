"""纯计算的恢复标准；缺数据、错误作用域和任一异常都不能成为恢复结论。"""

from app.connectors.cloud.models import CloudResources
from app.tools.kubernetes import KubernetesStatus, ServiceRuntime
from app.tools.observability import LogsOutput, MetricsOutput, TracesOutput
from app.verifier.models import VerificationConfig, VerificationSpec


def deployment_healthy(spec: VerificationSpec, data: KubernetesStatus) -> bool:
    if (data.cluster_name, data.namespace) != (spec.cluster_name, spec.namespace):
        return False
    if len(data.deployments) != 1:
        return False
    item = data.deployments[0]
    status = item.status
    return (
        item.metadata.name == spec.deployment_name
        and item.metadata.namespace == spec.namespace
        and item.metadata.generation is not None
        and status.observed_generation == item.metadata.generation
        and item.spec.replicas == spec.expected_replicas
        and status.replicas
        == status.updated_replicas
        == status.ready_replicas
        == status.available_replicas
        == spec.expected_replicas
        and any(c.type == "Available" and c.status == "True" for c in status.conditions)
        and not any(c.status == "False" for c in status.conditions)
    )


def pods_healthy(spec: VerificationSpec, data: ServiceRuntime) -> bool:
    if (data.cluster_name, data.namespace, data.service_name) != (
        spec.cluster_name,
        spec.namespace,
        spec.service_name,
    ) or len(data.pods) != spec.expected_replicas:
        return False
    for pod in data.pods:
        if pod.metadata.namespace != spec.namespace or pod.status.phase != "Running":
            return False
        if not any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions):
            return False
        by_name = {c.name: c for c in pod.status.container_statuses}
        if len(by_name) != len(pod.spec.containers) or not all(c.ready for c in by_name.values()):
            return False
        if set(by_name) != {c.name for c in pod.spec.containers}:
            return False
        targets = [c for c in pod.spec.containers if c.name == spec.container_name]
        if len(targets) != 1 or targets[0].image != spec.expected_image:
            return False
    return True


def metrics_healthy(
    spec: VerificationSpec, config: VerificationConfig, data: MetricsOutput, metric: str
) -> bool:
    if (data.service_name, data.start, data.end) != (spec.service_name, spec.start, spec.end):
        return False
    if not data.series:
        return False
    for series in data.series:
        if (series.service_name, series.metric_name) != (spec.service_name, metric):
            return False
        points = sorted(series.points, key=lambda p: p.timestamp)
        if len(points) < config.min_metric_points:
            return False
        timestamps = [p.timestamp for p in points]
        if len(set(timestamps)) != len(timestamps) or any(not spec.contains(t) for t in timestamps):
            return False
        if (
            (timestamps[0] - spec.start).total_seconds() > config.step_seconds
            or (spec.end - timestamps[-1]).total_seconds() > config.step_seconds
            or any(
                (b - a).total_seconds() > config.max_sample_gap_seconds
                for a, b in zip(timestamps, timestamps[1:], strict=False)
            )
        ):
            return False
        for point in points:
            if metric == "http_5xx_ratio" and not 0 <= point.value <= config.max_5xx_ratio:
                return False
            if metric == "http_p99_ms" and not 0 <= point.value <= config.max_p99_ms:
                return False
            if metric == "http_success_ratio" and not config.min_success_ratio <= point.value <= 1:
                return False
    return True


def logs_healthy(spec: VerificationSpec, data: LogsOutput) -> bool:
    return (data.service_name, data.start, data.end) == (
        spec.service_name,
        spec.start,
        spec.end,
    ) and all(
        item.service_name == spec.service_name
        and spec.contains(item.timestamp)
        and item.level.upper() in {"TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING"}
        for item in data.logs
    )


def traces_healthy(spec: VerificationSpec, config: VerificationConfig, data: TracesOutput) -> bool:
    if (data.service_name, data.start, data.end) != (spec.service_name, spec.start, spec.end):
        return False
    if not data.traces:
        return False
    for trace in data.traces:
        if (
            trace.service_name != spec.service_name
            or not spec.contains(trace.timestamp)
            or trace.duration_ms > config.max_p99_ms
            or not trace.spans
            or not any(span.service_name == spec.service_name for span in trace.spans)
        ):
            return False
        for span in trace.spans:
            code = span.result_code
            if not spec.contains(span.timestamp) or not (
                code.lower() in {"ok", "success", "0"}
                or (code.isdigit() and 200 <= int(code) < 300)
            ):
                return False
    return True


def resources_healthy(
    spec: VerificationSpec, config: VerificationConfig, data: CloudResources
) -> bool:
    if (data.service_name, data.start, data.end) != (spec.service_name, spec.start, spec.end):
        return False
    by_id = {r.identity: r for r in data.resources}
    for target in spec.resources:
        resource = by_id.get((target.product, target.region_id, target.resource_id))
        if resource is None or resource.status != target.healthy_status:
            return False
        sample = resource.rds_connections
        if resource.product == "rds":
            if (
                sample is None
                or sample.availability != "available"
                or sample.sampled_at is None
                or not spec.contains(sample.sampled_at)
                or (spec.end - sample.sampled_at).total_seconds() > config.max_sample_gap_seconds
                or sample.max_connections is None
                or sample.max_connections <= 0
                or sample.total_connections is None
                or sample.total_connections / sample.max_connections
                > config.max_rds_connection_ratio
            ):
                return False
    targets = {(r.product, r.region_id, r.resource_id) for r in spec.resources}
    return not any(
        (e.product, e.region_id, e.resource_id) in targets and e.level in {"CRITICAL", "WARN"}
        for e in data.events
    )
