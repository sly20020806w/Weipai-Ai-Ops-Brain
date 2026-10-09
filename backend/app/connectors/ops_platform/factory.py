"""通过 Step 10 工厂选型；真实分支必须显式提供源系统配置和 Reader 凭证。"""

import httpx2 as httpx

from app.config import Settings
from app.connectors.factory import ConnectorFactory
from app.connectors.models import ReaderCredentials
from app.connectors.ops_platform.client import HTTPOpsPlatformConnector, OpsPlatformConnector
from app.connectors.ops_platform.fake import FakeOpsPlatformConnector


def create_ops_platform_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> OpsPlatformConnector:
    # 使用经框架重新验证的独立配置，闭包不能读取可变的原 Settings。
    config = Settings.model_validate(
        {
            field.validation_alias if isinstance(field.validation_alias, str) else name: getattr(
                settings, name
            )
            for name, field in Settings.model_fields.items()
        }
    )

    def real(credentials: ReaderCredentials) -> OpsPlatformConnector:
        return HTTPOpsPlatformConnector(
            config.require_ops_platform_config(), credentials, transport=transport
        )

    return ConnectorFactory[OpsPlatformConnector](
        "ops_platform", fake=FakeOpsPlatformConnector, real=real
    ).create(config)
