"""周期触发配置只由环境变量注入；日历解释与计时交给 Temporal。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SchedulingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    enabled: bool = True
    schedule_prefix: str = Field(default="weipai-ops", pattern=r"^[a-z][a-z0-9-]{0,95}$")
    time_zone: Literal["Asia/Shanghai", "UTC"] = "Asia/Shanghai"
    service_name: str = Field(default="weipai-platform", min_length=1, max_length=256)
    inspection_hour: int = Field(default=9, ge=0, le=23)
    inspection_minute: int = Field(default=0, ge=0, le=59)
    inspection_weekdays: tuple[int, ...] = (1, 2, 3, 4, 5)
    capacity_minute: int = Field(default=0, ge=0, le=59)
    governance_hour: int = Field(default=18, ge=0, le=23)
    governance_minute: int = Field(default=0, ge=0, le=59)
    release_delay_seconds: int = Field(default=600, ge=1, le=86400)
    catchup_window_seconds: int = Field(default=3600, ge=1, le=86400)

    @field_validator("inspection_weekdays")
    @classmethod
    def weekdays(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or len(set(value)) != len(value) or any(day < 0 or day > 6 for day in value):
            raise ValueError("巡检星期必须为不重复的 0–6（0 为星期日）")
        return value

    @field_validator("service_name")
    @classmethod
    def service(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("定时任务服务范围不能包含空白边界或控制字符")
        return value
