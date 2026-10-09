"""配置只从 Settings 环境变量进入；Tool 不接受端点、项目或身份。"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.connectors.changes.models import non_secret_config_keys


class EndpointConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        hide_input_in_errors=True,
    )
    base_url: str
    services: dict[str, str] = Field(min_length=1)
    timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=100, ge=1, le=1000)

    @field_validator("base_url")
    @classmethod
    def url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.port != 0
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and "\\" not in value
                and "%" not in parsed.path
                and not any(char.isspace() or ord(char) < 32 for char in value)
                and all(part not in {".", ".."} for part in parsed.path.split("/"))
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("变更链路 base_url 必须是无凭证的 HTTPS 地址")
        return value.rstrip("/") + "/"

    @field_validator("services")
    @classmethod
    def bindings(cls, value: dict[str, str]) -> dict[str, str]:
        from pydantic import TypeAdapter

        from app.connectors.changes.models import ServiceName

        for name, target in value.items():
            TypeAdapter(ServiceName).validate_python(name)
            if (
                not target
                or len(target) > 256
                or any(char.isspace() or ord(char) < 32 for char in target)
                or any(part in {"", ".", ".."} for part in target.split("/"))
                or any(char in target for char in "\\?#%:")
            ):
                raise ValueError("服务绑定必须是无路径跳转的源系统标识符")
        return value


class GitConfig(EndpointConfig):
    provider: Literal["gitlab", "github"]


class CIConfig(EndpointConfig):
    provider: Literal["jenkins", "gitlab_ci"]
    username: str | None = Field(default=None, min_length=1, max_length=256)


class ArgoCDConfig(EndpointConfig):
    pass


class ConfigCenterConfig(EndpointConfig):
    # 未指定公司产品：明确的版本化 GET/JSON 适配协议，不假定 Nacos/Apollo API。
    allowed_keys: tuple[str, ...] = Field(min_length=1)

    @field_validator("allowed_keys")
    @classmethod
    def non_secret_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return non_secret_config_keys(value)
