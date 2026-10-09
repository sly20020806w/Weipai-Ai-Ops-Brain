"""复用配置工厂与 local/test Fake 门禁。"""

import httpx2 as httpx

from app.config import Settings
from app.connectors.factory import ConnectorFactory
from app.connectors.kubernetes.client import HTTPKubernetesConnector, KubernetesConnector
from app.connectors.kubernetes.fake import FakeKubernetesConnector
from app.connectors.models import ReaderCredentials


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

    return ConnectorFactory[KubernetesConnector]("kubernetes", fake=fake, real=real).create(config)
