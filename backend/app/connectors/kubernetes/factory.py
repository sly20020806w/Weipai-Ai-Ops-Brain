"""复用配置工厂与 local/test Fake 门禁。"""

from pathlib import Path

import httpx2 as httpx
from pydantic import SecretStr

from app.config import Settings
from app.connectors.factory import ConnectorFactory
from app.connectors.kubernetes.client import HTTPKubernetesConnector, KubernetesConnector
from app.connectors.kubernetes.fake import FakeKubernetesConnector
from app.connectors.models import ConnectorMode, ReaderCredentials


def create_kubernetes_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> KubernetesConnector:
    config = Settings.model_validate(
        {
            field.validation_alias if isinstance(field.validation_alias, str) else name: getattr(
                settings, name
            )
            for name, field in Settings.model_fields.items()
        }
    )

    def real(credentials: ReaderCredentials) -> KubernetesConnector:
        return HTTPKubernetesConnector(
            config.require_kubernetes_config(), credentials, transport=transport
        )

    def fake() -> KubernetesConnector:
        if config.kubernetes_config is None:
            return FakeKubernetesConnector()
        return FakeKubernetesConnector(
            cluster_name=config.kubernetes_config.cluster_name,
            service_label_key=config.kubernetes_config.service_label_key,
        )

    if config.connector_mode is ConnectorMode.REAL and config.kubernetes_reader_token_file:
        if "kubernetes" in config.connector_reader_tokens:
            raise ValueError("Kubernetes Reader 禁止同时配置投射 token 与环境 token")
        if not config.kubernetes_reader_ca_file:
            raise ValueError("Kubernetes 投射 Reader token 必须同时提供 CA 文件")
        try:
            token = Path(config.kubernetes_reader_token_file).read_text(encoding="utf-8").strip()
            ca = Path(config.kubernetes_reader_ca_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ValueError("Kubernetes 投射 Reader token 或 CA 无法读取") from None
        # 每次 Activity 创建 Connector 都重读 token，兼容 kubelet 的短时凭证轮换。
        kubernetes_config = config.require_kubernetes_config().model_copy(
            update={"ca_cert_pem": ca}
        )
        return HTTPKubernetesConnector(
            kubernetes_config,
            ReaderCredentials(connector="kubernetes", token=SecretStr(token)),
            transport=transport,
        )
    return ConnectorFactory[KubernetesConnector]("kubernetes", fake=fake, real=real).create(config)
