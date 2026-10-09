"""宿主配置的 GET/JSON 适配协议；路径必须由公司接口说明确认。"""

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OpsPlatformConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", hide_input_in_errors=True
    )

    base_url: str
    service_tree_path: str
    applications_path: str
    owners_path: str
    tickets_path: str
    timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)
    page_size: int = Field(default=100, ge=1, le=500)
    max_pages: int = Field(default=100, ge=1, le=1000)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.port != 0
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and not any(char.isspace() or ord(char) < 32 for char in value)
                and "\\" not in value
                and not any(segment in {".", ".."} for segment in url.path.split("/"))
                and "%" not in url.path
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("OPS_PLATFORM_CONFIG.base_url 必须是无凭证的 HTTPS 地址")
        return value.rstrip("/") + "/"

    @field_validator("service_tree_path", "applications_path", "owners_path", "tickets_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)*", value) is None:
            raise ValueError("OPS_PLATFORM_CONFIG 的路径必须是无查询参数的相对路径")
        return value
