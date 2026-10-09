"""HolmesGPT 与本平台之间的快照意见契约。"""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.models import JsonObject, ToolModel


class HolmesConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    base_url: str
    model: str = Field(min_length=1)
    snapshot_only: Literal[True]
    timeout_seconds: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.port != 0
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and not any(char.isspace() for char in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("HOLMES_CONFIG.base_url 必须是无凭证的 HTTP(S) 地址")
        return value.rstrip("/") + "/"

    @field_validator("model")
    @classmethod
    def model_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Holmes 模型配置名不能为空")
        return value


class HolmesRequest(ToolModel):
    service_name: str
    question: str
    context: JsonObject
    output_schema: JsonObject


class HolmesResponse(ToolModel):
    analysis: Annotated[str, Field(min_length=1, max_length=100000)]
