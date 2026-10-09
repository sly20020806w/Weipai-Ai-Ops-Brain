"""默认 Fake；local/test 禁止真实 Holmes 服务。"""

import httpx2 as httpx

from app.config import Settings
from app.connectors.factory import ConnectorFactory
from app.connectors.holmes.client import HolmesConnector, HTTPHolmesConnector
from app.connectors.holmes.fake import FakeHolmesConnector
from app.connectors.holmes.models import HolmesConfig
from app.connectors.models import ReaderCredentials


def create_holmes_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> HolmesConnector:
    config = Settings.model_validate(
        {
            field.validation_alias if isinstance(field.validation_alias, str) else name: getattr(
                settings, name
            )
            for name, field in Settings.model_fields.items()
        }
    )

    def real(credentials: ReaderCredentials) -> HolmesConnector:
        if config.holmes_config is None:
            raise ValueError("请通过环境变量设置 HOLMES_CONFIG")
        return HTTPHolmesConnector(
            HolmesConfig.model_validate(config.holmes_config), credentials, transport=transport
        )

    return ConnectorFactory[HolmesConnector]("holmes", fake=FakeHolmesConnector, real=real).create(
        config
    )
