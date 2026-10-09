"""端点与服务绑定只由环境配置提供，Agent 不能传入身份或云 API 动作。"""

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.connectors.cloud.models import APIProduct, Product, RegionID, ResourceID, ServiceName


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", hide_input_in_errors=True
    )


class ResourceBinding(ConfigModel):
    product: Product
    resource_id: ResourceID
    region_id: RegionID
    # CMS 有的产品返回 ARN；必须绑定准确值，不能用模糊包含判断服务归属。
    event_resource_id: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("event_resource_id")
    @classmethod
    def event_id(cls, value: str | None) -> str | None:
        if value is not None and any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("云事件资源标识不能包含空白或控制字符")
        return value

    @model_validator(mode="after")
    def domain(self) -> "ResourceBinding":
        if self.product in {"dns", "cdn"}:
            labels = self.resource_id.split(".")
            if (
                len(labels) < 2
                or len(self.resource_id) > 253
                or any(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) is None for x in labels
                )
            ):
                raise ValueError("DNS/CDN 资源必须是小写 ASCII 域名（中文用 punycode）")
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.product, self.region_id, self.resource_id


class CloudConfig(ConfigModel):
    endpoints: dict[APIProduct, str] = Field(min_length=1)
    services: dict[ServiceName, tuple[ResourceBinding, ...]] = Field(min_length=1)
    timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=100, ge=1, le=1000)

    @field_validator("endpoints")
    @classmethod
    def urls(cls, values: dict[APIProduct, str]) -> dict[APIProduct, str]:
        result: dict[APIProduct, str] = {}
        for product, value in values.items():
            try:
                url = urlsplit(value)
                valid = (
                    url.scheme == "https"
                    and bool(url.hostname)
                    and url.port in {None, 443}
                    and url.username is None
                    and url.password is None
                    and url.path in {"", "/"}
                    and not url.query
                    and not url.fragment
                    and "\\" not in value
                    and "%" not in value
                    and not any(char.isspace() or ord(char) < 32 for char in value)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("阿里云端点必须是无凭证、路径和查询参数的 HTTPS 地址")
            result[product] = value.rstrip("/") + "/"
        return result

    @model_validator(mode="after")
    def bindings(self) -> "CloudConfig":
        if "cms" not in self.endpoints:
            raise ValueError("CLOUD_CONFIG 必须包含 cms 云事件端点")
        for name, bindings in self.services.items():
            TypeAdapter(ServiceName).validate_python(name)
            if len(bindings) > 100:
                raise ValueError("每个服务最多绑定 100 个云资源")
            if len({binding.identity for binding in bindings}) != len(bindings):
                raise ValueError("同一服务的云资源绑定不能重复")
            event_ids = [(b.region_id, b.event_resource_id or b.resource_id) for b in bindings]
            if len(set(event_ids)) != len(event_ids):
                raise ValueError("同一服务的云事件资源绑定不能歧义")
            if any(binding.product not in self.endpoints for binding in bindings):
                raise ValueError("CLOUD_CONFIG 缺少绑定资源的产品端点")
        return self
