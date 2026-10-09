"""检测目标、基线和阈值只从环境变量配置，不写入数据库。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.connectors.kubernetes.models import Namespace, ServiceName
from app.connectors.observability.models import MetricName


class RuleModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")


class StateRule(RuleModel):
    rule_id: str = Field(default="replicas", pattern=r"^[a-z][a-z0-9-]{0,63}$")
    service_name: ServiceName = "payment-service"
    namespace: Namespace = "payment"
    baseline_replicas: int = Field(default=3, ge=0, le=100000)
    desired_replicas: int | None = Field(default=None, ge=0, le=100000)
    allowed_deficit: int = Field(default=0, ge=0, le=100000)


class TrendRule(RuleModel):
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    service_name: ServiceName = "payment-service"
    metric_name: MetricName
    kind: Literal["capacity", "traffic", "cost", "bottleneck"]
    limit: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    baseline: float = Field(default=100.0, gt=0, allow_inf_nan=False)
    growth_fraction: float = Field(default=0.5, gt=0, allow_inf_nan=False)
    horizon_seconds: int = Field(default=3600, ge=60, le=604800)
    min_points: int = Field(default=3, ge=3, le=1000)
    min_span_seconds: int = Field(default=120, ge=1, le=86400)
    min_r_squared: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)
    max_age_seconds: int = Field(default=120, ge=1, le=86400)


def default_trends() -> tuple[TrendRule, ...]:
    return (
        TrendRule(rule_id="disk", metric_name="disk_used_ratio", kind="capacity"),
        TrendRule(rule_id="traffic", metric_name="http_requests_rate", kind="traffic"),
        TrendRule(rule_id="cost", metric_name="daily_cost", kind="cost"),
        TrendRule(rule_id="cpu", metric_name="cpu_usage_ratio", kind="bottleneck", limit=0.9),
    )


class DetectionConfig(RuleModel):
    enabled: bool = True
    schedule_id: str = Field(default="weipai-state-prediction", pattern=r"^[a-z][a-z0-9-]{0,95}$")
    interval_seconds: int = Field(default=300, ge=1, le=86400)
    lookback_seconds: int = Field(default=900, ge=1, le=86400)
    step_seconds: int = Field(default=60, ge=1, le=3600)
    activity_timeout_seconds: int = Field(default=60, ge=1, le=600)
    activity_max_attempts: int = Field(default=3, ge=1, le=10)
    state_rules: tuple[StateRule, ...] = (StateRule(),)
    trend_rules: tuple[TrendRule, ...] = Field(default_factory=default_trends)

    @model_validator(mode="after")
    def valid_rules(self) -> "DetectionConfig":
        rules: tuple[StateRule | TrendRule, ...] = (*self.state_rules, *self.trend_rules)
        ids = [rule.rule_id for rule in rules]
        if len(ids) != len(set(ids)) or len(ids) > 100:
            raise ValueError("检测规则 ID 必须唯一，且总数不超过 100")
        if self.lookback_seconds / self.step_seconds > 11000:
            raise ValueError("检测时间窗的采样点超过上限")
        if any(rule.min_span_seconds >= self.lookback_seconds for rule in self.trend_rules):
            raise ValueError("趋势最小跨度必须小于查询时间窗")
        return self
