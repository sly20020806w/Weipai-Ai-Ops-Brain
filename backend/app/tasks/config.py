"""Temporal 运行参数只从 Settings 环境配置注入。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TemporalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    address: str = "127.0.0.1:7233"
    namespace: str = Field(default="default", min_length=1)
    task_queue: str = Field(default="weipai-ai-tasks", min_length=1)
    human_timeout_seconds: float = Field(default=3600, gt=0, allow_inf_nan=False)
    activity_timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    activity_max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        host, separator, port = value.rpartition(":")
        if (
            not separator
            or not host
            or not port.isascii()
            or not port.isdecimal()
            or not 1 <= int(port) <= 65535
            or any(char.isspace() for char in value)
            or any(char in value for char in "/@?#")
        ):
            raise ValueError("TEMPORAL_CONFIG.address 必须是无凭证的 host:port")
        return value

    @field_validator("namespace", "task_queue")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip() or not value.strip():
            raise ValueError("Temporal namespace/task_queue 不能为空或带首尾空白")
        return value
