"""显式配置 ACK/K8s API 和服务标签；身份只来自环境变量。"""

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.connectors.kubernetes.models import Namespace


class KubernetesConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", hide_input_in_errors=True
    )
    cluster_name: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    base_url: str
    service_label_key: str = "app.kubernetes.io/name"
    ca_cert_pem: str | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)
    page_size: int = Field(default=100, ge=1, le=500)
    max_pages: int = Field(default=100, ge=1, le=1000)
    namespace_allowlist: tuple[Namespace, ...] | None = Field(
        default=None, min_length=1, max_length=1000
    )

    @field_validator("namespace_allowlist")
    @classmethod
    def unique_namespaces(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("命名空间白名单不能重复")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
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
                and url.path in {"", "/"}
                and not any(char.isspace() or ord(char) < 32 for char in value)
                and "\\" not in value
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("KUBERNETES_CONFIG.base_url 必须是无凭证、无路径的 HTTPS API 地址")
        return value.rstrip("/") + "/"

    @field_validator("service_label_key")
    @classmethod
    def validate_label_key(cls, value: str) -> str:
        parts = value.split("/")
        name = parts[-1]
        valid = (
            len(parts) <= 2
            and len(name) <= 63
            and re.fullmatch(r"[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?", name) is not None
        )
        if len(parts) == 2:
            valid = (
                valid
                and len(parts[0]) <= 253
                and all(
                    len(segment) <= 63 and re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", segment)
                    for segment in parts[0].split(".")
                )
            )
        if not valid:
            raise ValueError("KUBERNETES_CONFIG.service_label_key 必须是合法的 Kubernetes 标签键")
        return value
