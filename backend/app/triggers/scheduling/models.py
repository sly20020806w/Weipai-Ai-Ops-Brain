"""确定性 Workflow 的输入只包含非敏感触发参数与源事件引用。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

PeriodicKind = Literal["workday-inspection", "hourly-capacity", "daily-governance"]
PERIODIC_TITLES: dict[PeriodicKind, str] = {
    "workday-inspection": "开工巡检",
    "hourly-capacity": "每小时容量检查",
    "daily-governance": "每日资源治理",
}


@dataclass(frozen=True)
class PeriodicInput:
    kind: PeriodicKind
    service_name: str = "weipai-platform"


@dataclass(frozen=True)
class ReleaseVerificationInput:
    event_id: str
    service_name: str
    occurred_at: datetime
    delay_seconds: int = 600
