"""默认 Fake；复用 local/test 门禁和 Reader 身份工厂。"""

import httpx2 as httpx

from app.config import Settings
from app.connectors.cloud.base import CloudConnector
from app.connectors.cloud.client import HTTPCloudConnector
from app.connectors.cloud.fake import FakeCloudConnector
from app.connectors.factory import ConnectorFactory
from app.connectors.models import ReaderCredentials


def create_cloud_connector(
    settings: Settings,
    *,
    transport: httpx.MockTransport | None = None,
) -> CloudConnector:
    def real(credentials: ReaderCredentials) -> CloudConnector:
        return HTTPCloudConnector(settings.require_cloud_config(), credentials, transport=transport)

    return ConnectorFactory[CloudConnector]("cloud", fake=FakeCloudConnector, real=real).create(
        settings
    )
