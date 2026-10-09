"""按 CONNECTOR_MODE 选择通知通道；不使用只读工厂或生产动作凭证。"""

import httpx2 as httpx

from app.config import Settings
from app.connectors.feishu.base import FeishuConnector
from app.connectors.feishu.client import HTTPFeishuConnector
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.models import ConnectorMode


def create_feishu_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> FeishuConnector:
    config = Settings.model_validate(
        {
            field.validation_alias if isinstance(field.validation_alias, str) else name: getattr(
                settings, name
            )
            for name, field in Settings.model_fields.items()
        }
    )
    if config.connector_mode is ConnectorMode.FAKE:
        # Fake 不读取真实通知凭证，也不构建 HTTP 客户端。
        return FakeFeishuConnector()
    if config.feishu_notification_credentials is None:
        raise ValueError("请通过环境变量设置 FEISHU_NOTIFICATION_CREDENTIALS")
    return HTTPFeishuConnector(
        config.require_feishu_config(), config.feishu_notification_credentials, transport=transport
    )
