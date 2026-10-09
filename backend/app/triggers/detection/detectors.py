"""纯计算检测器；线性外推仅用于生成调查任务，不给出根因或执行动作。"""

import json
from datetime import datetime, timedelta
from hashlib import sha256
from math import fsum, isfinite

from pydantic import JsonValue

from app.connectors.kubernetes.models import Deployment
from app.connectors.observability.models import MetricSeries, MetricsQuery
from app.tasks.states import TaskSource
from app.triggers.detection.config import StateRule, TrendRule
from app.triggers.detection.models import Observation


def detector_key(rule: StateRule | TrendRule, target: str) -> str:
    return sha256(
        json.dumps([rule.model_dump(mode="json"), target], sort_keys=True).encode()
    ).hexdigest()


def detect_state(
    rule: StateRule,
    deployment: Deployment | tuple[Deployment, ...] | None,
    cluster: str,
    at: datetime,
) -> Observation | None:
    rule = StateRule.model_validate(rule)
    items = (
        ()
        if deployment is None
        else (deployment,)
        if isinstance(deployment, Deployment)
        else deployment
    )
    items = tuple(Deployment.model_validate(item) for item in items)
    if len({item.metadata.uid for item in items}) != len(items):
        raise ValueError("副本检测不能重复计算同一 Deployment")
    current = sum(item.status.ready_replicas for item in items)
    desired = rule.desired_replicas
    if desired is None:
        desired = sum(item.spec.replicas for item in items) if items else rule.baseline_replicas
    for item in items:
        if item.metadata.namespace != rule.namespace:
            raise ValueError("检测对象命名空间不匹配")
        # 未观测新 generation 时无法可靠比较当前状态，不把未知当成恢复。
        generation, observed = item.metadata.generation, item.status.observed_generation
        if generation is not None and (observed is None or observed < generation):
            return None
    target = f"{cluster}/{rule.namespace}/{rule.service_name}"
    desired_deficit = max(0, desired - current)
    baseline_deficit = max(0, rule.baseline_replicas - current)
    return Observation(
        detector_key=detector_key(rule, target),
        source=TaskSource.STATE,
        service_name=rule.service_name,
        observed_at=at,
        breached=max(desired_deficit, baseline_deficit) > rule.allowed_deficit,
        title=(
            f"状态调查：{rule.service_name} 可用副本 {current}，"
            f"期望 {desired}，基线 {rule.baseline_replicas}"
        ),
        source_reference=f"kubernetes:{target}",
        summary={
            "kind": "replicas",
            "current": current,
            "baseline": rule.baseline_replicas,
            "desired": desired,
            "allowed_deficit": rule.allowed_deficit,
            "deployment_uids": [item.metadata.uid for item in items],
            "missing": not items,
        },
    )


def detect_trend(rule: TrendRule, series: MetricSeries, query: MetricsQuery) -> Observation | None:
    rule = TrendRule.model_validate(rule)
    series = MetricSeries.model_validate(series)
    query = MetricsQuery.model_validate(query)
    if series.service_name != rule.service_name or series.metric_name != rule.metric_name:
        raise ValueError("趋势序列与检测规则不匹配")
    points = sorted(
        (p for p in series.points if query.contains(p.timestamp)), key=lambda p: p.timestamp
    )
    if len({p.timestamp for p in points}) != len(points):
        raise ValueError("趋势采样时间不能重复")
    if len(points) < rule.min_points:
        return None
    first, last = points[0], points[-1]
    span = (last.timestamp - first.timestamp).total_seconds()
    if (
        span < rule.min_span_seconds
        or (query.end - last.timestamp).total_seconds() > rule.max_age_seconds
    ):
        return None
    if any(p.value < 0 for p in points):
        raise ValueError("检测指标必须为非负值")
    xs = [(p.timestamp - first.timestamp).total_seconds() for p in points]
    ys = [p.value for p in points]
    mx, my = fsum(xs) / len(xs), fsum(ys) / len(ys)
    variance = fsum((x - mx) ** 2 for x in xs)
    slope = fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / variance
    residual = fsum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys, strict=True))
    total = fsum((y - my) ** 2 for y in ys)
    # 常量小数的均值可能有舍入误差；常量序列的拟合质量应为 1。
    r_squared = max(0.0, min(1.0, 1.0 - residual / total)) if max(ys) > min(ys) else 1.0
    forecast = last.value + slope * rule.horizon_seconds
    if not all(isfinite(value) for value in (slope, r_squared, forecast)):
        raise ValueError("趋势计算超出有效数值范围")
    reliable = r_squared >= rule.min_r_squared
    exhausted_at: datetime | None = None
    if rule.kind == "capacity":
        remaining = (rule.limit - last.value) / slope if slope > 0 else float("inf")
        breached = last.value >= rule.limit or (reliable and 0 <= remaining <= rule.horizon_seconds)
        if breached:
            exhausted_at = (
                last.timestamp
                if last.value >= rule.limit
                else last.timestamp + timedelta(seconds=remaining)
            )
    elif rule.kind in {"traffic", "cost"}:
        ceiling = rule.baseline * (1 + rule.growth_fraction)
        breached = last.value > ceiling or (reliable and slope > 0 and forecast > ceiling)
    else:
        sustained = all(p.value >= rule.limit for p in points[-rule.min_points :])
        breached = sustained or (reliable and slope > 0 and forecast >= rule.limit)
    # 噪声序列不能证明恢复；持续超限和当前已超限仍可生成调查。
    if not breached and not reliable:
        return None
    labels = json.dumps(series.labels, sort_keys=True, ensure_ascii=False)
    target = f"{rule.service_name}/{rule.metric_name}/{labels}"
    summary: dict[str, JsonValue] = {
        "kind": rule.kind,
        "metric_name": rule.metric_name,
        "labels": dict(series.labels),
        "current": last.value,
        "baseline": rule.baseline,
        "limit": rule.limit,
        "growth_fraction": rule.growth_fraction,
        "horizon_seconds": rule.horizon_seconds,
        "slope_per_second": slope,
        "r_squared": r_squared,
        "forecast": forecast,
        "predicted_exhaustion_at": exhausted_at.isoformat() if exhausted_at else None,
        "sample_count": len(points),
        "sample_start": first.timestamp.isoformat(),
        "sample_end": last.timestamp.isoformat(),
    }
    names = {
        "capacity": "容量耗尽",
        "traffic": "流量增长",
        "cost": "成本异常",
        "bottleneck": "资源瓶颈",
    }
    title = f"趋势调查：{rule.service_name} {names[rule.kind]}/{rule.metric_name}"
    if exhausted_at:
        title += f"，预计耗尽 {exhausted_at.isoformat()}"
    return Observation(
        detector_key=detector_key(rule, target),
        source=TaskSource.PREDICTION,
        service_name=rule.service_name,
        observed_at=last.timestamp,
        breached=breached,
        title=title,
        source_reference=f"prometheus:{rule.service_name}/{rule.metric_name}",
        summary=summary,
    )
