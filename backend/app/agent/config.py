"""主 Agent 参数由环境变量注入，不接受模型提供的执行权限。"""

from pydantic import BaseModel, ConfigDict, Field


class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    max_steps: int = Field(default=20, ge=1, le=100, strict=True)
    experts_enabled: bool = True
    expert_max_steps: int = Field(default=8, ge=1, le=30, strict=True)
    reviewer_max_steps: int = Field(default=16, ge=1, le=100, strict=True)
