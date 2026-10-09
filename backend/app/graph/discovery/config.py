"""Discovery 的周期来自环境配置，Schedule 持久化在 Temporal。"""

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")
    schedule_id: str = Field(default="weipai-discovery", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    interval_seconds: int = Field(default=300, ge=1, le=86400)
    lookback_seconds: int = Field(default=900, ge=1, le=86400)
    activity_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    activity_max_attempts: int = Field(default=3, ge=1, le=10)
