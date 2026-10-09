"""只从环境变量读取配置；不加载本地配置文件。"""

import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from app.agent.config import AgentConfig
from app.auth.config import AuthConfig
from app.connectors.changes.config import ArgoCDConfig, CIConfig, ConfigCenterConfig, GitConfig
from app.connectors.cloud.config import CloudConfig
from app.connectors.feishu.config import FeishuConfig, FeishuNotificationCredentials
from app.connectors.holmes.models import HolmesConfig
from app.connectors.inspection.client import InspectionEndpoint
from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.models import ConnectorMode, ReaderCredentials
from app.connectors.observability.config import ARMSConfig, PrometheusConfig, SLSConfig
from app.connectors.ops_platform.config import OpsPlatformConfig
from app.executor.models import ExecutionConfig
from app.graph.discovery.config import DiscoveryConfig
from app.learning.automation.models import AutomationConfig
from app.policy.models import PolicyConfig
from app.runbooks.maturity import MaturityConfig
from app.tasks.config import TemporalConfig
from app.tasks.inspection.models import InspectionConfig
from app.tasks.releases.models import ReleaseConfig
from app.tasks.safety.models import SafetyConfig
from app.tasks.tickets.models import TicketConfig
from app.tasks.war_room.models import WarRoomConfig
from app.triggers.config import TriggerConfig
from app.triggers.detection.config import DetectionConfig
from app.triggers.scheduling.config import SchedulingConfig
from app.verifier.models import VerificationConfig


def parse_database_url(value: str) -> URL:
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError("DATABASE_URL 必须是有效的 PostgreSQL 异步连接地址") from None
    if url.drivername != "postgresql+asyncpg" or not url.host or not url.database:
        raise ValueError("DATABASE_URL 必须使用 postgresql+asyncpg 并指定主机和数据库")
    return url


class GatewayConfig(BaseModel):
    """只连接显式配置的公司网关；没有公共模型服务的缺省地址。"""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    base_url: str
    api_key: SecretStr
    chat_model: str = Field(min_length=1)
    embedding_model: str = Field(min_length=1)
    timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_delay_seconds: float = Field(default=0.5, ge=0, le=30, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
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
                and not any(character.isspace() for character in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("AI_GATEWAY_BASE_URL 必须是无凭证、查询参数或片段的 HTTP(S) 地址")
        return value.rstrip("/") + "/"

    @field_validator("chat_model", "embedding_model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("AI_GATEWAY_CHAT_MODEL 和 AI_GATEWAY_EMBEDDING_MODEL 不能为空")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value() or any(
            character.isspace() for character in value.get_secret_value()
        ):
            raise ValueError("AI_GATEWAY_API_KEY 不能为空或包含空白字符")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, case_sensitive=True, extra="ignore", hide_input_in_errors=True
    )

    @field_validator("ticket_config", mode="before")
    @classmethod
    def parse_ticket_config(cls, value: object) -> object:
        if isinstance(value, dict):
            return TicketConfig.model_validate_json(json.dumps(value))
        return value

    @field_validator("inspection_config", mode="before")
    @classmethod
    def parse_inspection_config(cls, value: object) -> object:
        if isinstance(value, dict):
            return InspectionConfig.model_validate_json(json.dumps(value))
        return value

    app_env: Literal["local", "test", "staging", "production"] = Field(validation_alias="APP_ENV")
    api_host: str = Field(default="127.0.0.1", validation_alias="API_HOST", min_length=1)
    api_port: int = Field(default=8000, validation_alias="API_PORT", ge=1, le=65535)
    auth_config: AuthConfig | None = Field(default=None, validation_alias="AUTH_CONFIG", repr=False)
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")
    agent_config: AgentConfig = Field(default_factory=AgentConfig, validation_alias="AGENT_CONFIG")
    chat_stream_timeout_seconds: float = Field(
        default=120,
        ge=1,
        le=600,
        allow_inf_nan=False,
        validation_alias="CHAT_STREAM_TIMEOUT_SECONDS",
    )
    temporal_config: TemporalConfig = Field(
        default_factory=TemporalConfig, validation_alias="TEMPORAL_CONFIG"
    )
    trigger_config: TriggerConfig = Field(
        default_factory=TriggerConfig, validation_alias="TRIGGER_CONFIG"
    )
    scheduling_config: SchedulingConfig = Field(
        default_factory=SchedulingConfig, validation_alias="SCHEDULING_CONFIG"
    )
    detection_config: DetectionConfig = Field(
        default_factory=DetectionConfig, validation_alias="DETECTION_CONFIG"
    )
    discovery_config: DiscoveryConfig = Field(
        default_factory=DiscoveryConfig, validation_alias="DISCOVERY_CONFIG"
    )
    policy_config: PolicyConfig = Field(
        default_factory=PolicyConfig, validation_alias="POLICY_CONFIG"
    )
    runbook_maturity_config: MaturityConfig = Field(
        default_factory=MaturityConfig, validation_alias="RUNBOOK_MATURITY_CONFIG"
    )
    verification_config: VerificationConfig = Field(
        default_factory=VerificationConfig, validation_alias="VERIFICATION_CONFIG"
    )
    execution_config: ExecutionConfig = Field(
        default_factory=ExecutionConfig, validation_alias="EXECUTION_CONFIG"
    )
    safety_config: SafetyConfig = Field(
        default_factory=SafetyConfig, validation_alias="SAFETY_CONFIG"
    )
    automation_config: AutomationConfig = Field(
        default_factory=AutomationConfig, validation_alias="AUTOMATION_CONFIG"
    )
    ticket_config: TicketConfig = Field(
        default_factory=TicketConfig, validation_alias="TICKET_CONFIG"
    )
    release_config: ReleaseConfig = Field(
        default_factory=ReleaseConfig, validation_alias="RELEASE_CONFIG"
    )
    inspection_config: InspectionConfig = Field(
        default_factory=InspectionConfig, validation_alias="INSPECTION_CONFIG"
    )
    war_room_config: WarRoomConfig = Field(
        default_factory=WarRoomConfig, validation_alias="WAR_ROOM_CONFIG"
    )
    inspection_endpoint: InspectionEndpoint | None = Field(
        default=None, validation_alias="INSPECTION_ENDPOINT"
    )
    connector_mode: ConnectorMode = Field(
        default=ConnectorMode.FAKE, validation_alias="CONNECTOR_MODE"
    )
    connector_reader_tokens: dict[str, SecretStr] = Field(
        default_factory=dict, validation_alias="CONNECTOR_READER_TOKENS", repr=False
    )
    ops_platform_config: OpsPlatformConfig | None = Field(
        default=None, validation_alias="OPS_PLATFORM_CONFIG"
    )
    kubernetes_config: KubernetesConfig | None = Field(
        default=None, validation_alias="KUBERNETES_CONFIG"
    )
    prometheus_config: PrometheusConfig | None = Field(
        default=None, validation_alias="PROMETHEUS_CONFIG"
    )
    sls_config: SLSConfig | None = Field(default=None, validation_alias="SLS_CONFIG")
    arms_config: ARMSConfig | None = Field(default=None, validation_alias="ARMS_CONFIG")
    git_config: GitConfig | None = Field(default=None, validation_alias="GIT_CONFIG")
    ci_config: CIConfig | None = Field(default=None, validation_alias="CI_CONFIG")
    argocd_config: ArgoCDConfig | None = Field(default=None, validation_alias="ARGOCD_CONFIG")
    config_center_config: ConfigCenterConfig | None = Field(
        default=None, validation_alias="CONFIG_CENTER_CONFIG"
    )
    cloud_config: CloudConfig | None = Field(default=None, validation_alias="CLOUD_CONFIG")
    holmes_config: HolmesConfig | None = Field(default=None, validation_alias="HOLMES_CONFIG")
    feishu_config: FeishuConfig | None = Field(default=None, validation_alias="FEISHU_CONFIG")
    feishu_notification_credentials: FeishuNotificationCredentials | None = Field(
        default=None, validation_alias="FEISHU_NOTIFICATION_CREDENTIALS", repr=False
    )
    llm_mode: Literal["fake", "gateway"] = Field(default="fake", validation_alias="LLM_MODE")
    ai_gateway_base_url: str | None = Field(default=None, validation_alias="AI_GATEWAY_BASE_URL")
    ai_gateway_api_key: SecretStr | None = Field(
        default=None, validation_alias="AI_GATEWAY_API_KEY"
    )
    ai_gateway_chat_model: str | None = Field(
        default=None, validation_alias="AI_GATEWAY_CHAT_MODEL"
    )
    ai_gateway_embedding_model: str | None = Field(
        default=None, validation_alias="AI_GATEWAY_EMBEDDING_MODEL"
    )
    ai_gateway_timeout_seconds: float = Field(
        default=30, gt=0, le=300, allow_inf_nan=False, validation_alias="AI_GATEWAY_TIMEOUT_SECONDS"
    )
    ai_gateway_max_retries: int = Field(
        default=2, ge=0, le=5, validation_alias="AI_GATEWAY_MAX_RETRIES"
    )
    ai_gateway_retry_delay_seconds: float = Field(
        default=0.5,
        ge=0,
        le=30,
        allow_inf_nan=False,
        validation_alias="AI_GATEWAY_RETRY_DELAY_SECONDS",
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            parse_database_url(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def restrict_local_database(self) -> "Settings":
        if self.database_url is not None and self.app_env in {"local", "test"}:
            if self.require_database_url().host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("local/test 环境的 DATABASE_URL 只允许本机回环地址")
        return self

    @model_validator(mode="after")
    def restrict_local_connectors(self) -> "Settings":
        if self.app_env in {"local", "test"} and self.connector_mode is ConnectorMode.REAL:
            raise ValueError("local/test 环境的 CONNECTOR_MODE 只允许 fake")
        return self

    @field_validator("connector_reader_tokens")
    @classmethod
    def validate_reader_tokens(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        for name, token in value.items():
            ReaderCredentials(connector=name, token=token)
        return value

    @model_validator(mode="after")
    def validate_gateway_mode(self) -> "Settings":
        if self.llm_mode == "gateway":
            self.require_gateway_config()
        return self

    def require_gateway_config(self) -> GatewayConfig:
        values = {
            "AI_GATEWAY_BASE_URL": self.ai_gateway_base_url,
            "AI_GATEWAY_API_KEY": self.ai_gateway_api_key,
            "AI_GATEWAY_CHAT_MODEL": self.ai_gateway_chat_model,
            "AI_GATEWAY_EMBEDDING_MODEL": self.ai_gateway_embedding_model,
        }
        missing = [name for name, value in values.items() if value is None]
        if missing:
            raise ValueError(f"请通过环境变量设置 {', '.join(missing)}")
        assert self.ai_gateway_base_url is not None
        assert self.ai_gateway_api_key is not None
        assert self.ai_gateway_chat_model is not None
        assert self.ai_gateway_embedding_model is not None
        return GatewayConfig(
            base_url=self.ai_gateway_base_url,
            api_key=self.ai_gateway_api_key,
            chat_model=self.ai_gateway_chat_model,
            embedding_model=self.ai_gateway_embedding_model,
            timeout_seconds=self.ai_gateway_timeout_seconds,
            max_retries=self.ai_gateway_max_retries,
            retry_delay_seconds=self.ai_gateway_retry_delay_seconds,
        )

    def require_database_url(self) -> URL:
        if self.database_url is None:
            raise ValueError("请通过环境变量设置 DATABASE_URL")
        return parse_database_url(self.database_url.get_secret_value())

    def validate_api_auth(self) -> None:
        if self.app_env in {"staging", "production"}:
            if self.auth_config is None:
                raise ValueError("请通过环境变量设置 AUTH_CONFIG")
            if not self.auth_config.secure_cookie:
                raise ValueError("staging/production 的 AUTH_CONFIG.public_origin 必须使用 HTTPS")
            self.require_database_url()

    def require_ops_platform_config(self) -> OpsPlatformConfig:
        if self.ops_platform_config is None:
            raise ValueError("请通过环境变量设置 OPS_PLATFORM_CONFIG")
        return OpsPlatformConfig.model_validate(self.ops_platform_config)

    def require_kubernetes_config(self) -> KubernetesConfig:
        if self.kubernetes_config is None:
            raise ValueError("请通过环境变量设置 KUBERNETES_CONFIG")
        return KubernetesConfig.model_validate(self.kubernetes_config)

    def require_prometheus_config(self) -> PrometheusConfig:
        if self.prometheus_config is None:
            raise ValueError("请通过环境变量设置 PROMETHEUS_CONFIG")
        return PrometheusConfig.model_validate(self.prometheus_config)

    def require_sls_config(self) -> SLSConfig:
        if self.sls_config is None:
            raise ValueError("请通过环境变量设置 SLS_CONFIG")
        return SLSConfig.model_validate(self.sls_config)

    def require_arms_config(self) -> ARMSConfig:
        if self.arms_config is None:
            raise ValueError("请通过环境变量设置 ARMS_CONFIG")
        return ARMSConfig.model_validate(self.arms_config)

    def require_git_config(self) -> GitConfig:
        if self.git_config is None:
            raise ValueError("请通过环境变量设置 GIT_CONFIG")
        return GitConfig.model_validate(self.git_config)

    def require_ci_config(self) -> CIConfig:
        if self.ci_config is None:
            raise ValueError("请通过环境变量设置 CI_CONFIG")
        return CIConfig.model_validate(self.ci_config)

    def require_argocd_config(self) -> ArgoCDConfig:
        if self.argocd_config is None:
            raise ValueError("请通过环境变量设置 ARGOCD_CONFIG")
        return ArgoCDConfig.model_validate(self.argocd_config)

    def require_config_center_config(self) -> ConfigCenterConfig:
        if self.config_center_config is None:
            raise ValueError("请通过环境变量设置 CONFIG_CENTER_CONFIG")
        return ConfigCenterConfig.model_validate(self.config_center_config)

    def require_cloud_config(self) -> CloudConfig:
        if self.cloud_config is None:
            raise ValueError("请通过环境变量设置 CLOUD_CONFIG")
        return CloudConfig.model_validate(self.cloud_config.model_dump())

    def require_feishu_config(self) -> FeishuConfig:
        if self.feishu_config is None:
            raise ValueError("请通过环境变量设置 FEISHU_CONFIG")
        return FeishuConfig.model_validate(self.feishu_config)
