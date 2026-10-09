"""事件接入与 Watch 参数；凭证只通过环境变量注入。"""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class TriggerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    webhook_secrets: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    signature_max_age_seconds: int = Field(default=300, ge=1, le=3600)
    max_body_bytes: int = Field(default=262144, ge=1024, le=1048576)
    response_timeout_seconds: int = Field(default=15, ge=1, le=60)
    watcher_enabled: bool = False
    watcher_namespaces: tuple[str, ...] = ("payment",)
    watch_timeout_seconds: int = Field(default=20, ge=1, le=60)

    @field_validator("webhook_secrets")
    @classmethod
    def check_secrets(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        allowed = {
            "prometheus",
            "ops_platform",
            "git",
            "ci",
            "argocd",
            "config_center",
            "cloud",
            "manual",
        }
        if any(
            name not in allowed or len(secret.get_secret_value()) < 32
            for name, secret in value.items()
        ):
            raise ValueError("Webhook 来源必须受支持，且每个签名密钥至少 32 字符")
        return value

    @field_validator("watcher_namespaces")
    @classmethod
    def check_namespaces(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        from pydantic import TypeAdapter

        from app.connectors.kubernetes.models import Namespace

        if not value or len(value) != len(set(value)):
            raise ValueError("Watcher 命名空间必须非空且不能重复")
        for namespace in value:
            TypeAdapter(Namespace).validate_python(namespace)
        return value
