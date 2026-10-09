"""显式端点/字段映射来自环境变量；Alibaba 凭证保存在 Reader 的 SecretStr 中。"""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

FieldName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]


class EndpointConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", hide_input_in_errors=True
    )
    base_url: str
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
                and not any(char.isspace() or ord(char) < 32 for char in value)
                and all(part not in {".", ".."} for part in parsed.path.split("/"))
                and "%" not in parsed.path
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("可观测性 base_url 必须是无凭证的 HTTPS 地址")
        return value.rstrip("/") + "/"


class PrometheusConfig(EndpointConfig):
    service_label: FieldName = "service"
    max_series: int = Field(default=1000, ge=1, le=10000)


class SLSConfig(EndpointConfig):
    project: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    logstore: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    service_field: FieldName = "service_name"
    level_field: FieldName = "level"
    message_field: FieldName = "message"

    @field_validator("base_url")
    @classmethod
    def root_url(cls, value: str) -> str:
        if urlsplit(value).path != "/":
            raise ValueError("SLS base_url 必须是 project 端点且无路径")
        return value


class ARMSConfig(EndpointConfig):
    region_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    # ARMS Application Monitoring 与 XTrace 版本的时间单位不同，必须显式选定。
    span_timestamp_unit: Literal["milliseconds", "microseconds"] = "milliseconds"

    @field_validator("base_url")
    @classmethod
    def root_url(cls, value: str) -> str:
        if urlsplit(value).path != "/":
            raise ValueError("ARMS base_url 必须是无路径的 API 端点")
        return value


class AlibabaReaderKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    access_key_id: SecretStr
    access_key_secret: SecretStr
    security_token: SecretStr | None = None

    @field_validator("access_key_id", "access_key_secret", "security_token")
    @classmethod
    def nonempty(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and (
            not value.get_secret_value()
            or any(char.isspace() or ord(char) < 32 for char in value.get_secret_value())
        ):
            raise ValueError("Alibaba Reader 凭证不能为空或包含空白/控制字符")
        return value
