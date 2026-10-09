"""默认 Fake，真实分支复用已有 local/test 门禁和 Reader 凭证隔离。"""

import httpx2 as httpx

from app.config import Settings
from app.connectors.changes.argocd import HTTPArgoCDConnector
from app.connectors.changes.base import (
    ArgoCDConnector,
    CIConnector,
    ConfigCenterConnector,
    GitConnector,
)
from app.connectors.changes.ci import HTTPCIConnector
from app.connectors.changes.config_center import HTTPConfigCenterConnector
from app.connectors.changes.fake import (
    FakeArgoCDConnector,
    FakeCIConnector,
    FakeConfigCenterConnector,
    FakeGitConnector,
)
from app.connectors.changes.git import HTTPGitConnector
from app.connectors.factory import ConnectorFactory
from app.connectors.models import ReaderCredentials


def create_git_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> GitConnector:
    def real(credentials: ReaderCredentials) -> GitConnector:
        return HTTPGitConnector(settings.require_git_config(), credentials, transport=transport)

    return ConnectorFactory[GitConnector]("git", fake=FakeGitConnector, real=real).create(settings)


def create_ci_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> CIConnector:
    def real(credentials: ReaderCredentials) -> CIConnector:
        return HTTPCIConnector(settings.require_ci_config(), credentials, transport=transport)

    return ConnectorFactory[CIConnector]("ci", fake=FakeCIConnector, real=real).create(settings)


def create_argocd_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> ArgoCDConnector:
    def real(credentials: ReaderCredentials) -> ArgoCDConnector:
        return HTTPArgoCDConnector(
            settings.require_argocd_config(), credentials, transport=transport
        )

    return ConnectorFactory[ArgoCDConnector]("argocd", fake=FakeArgoCDConnector, real=real).create(
        settings
    )


def create_config_center_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> ConfigCenterConnector:
    def real(credentials: ReaderCredentials) -> ConfigCenterConnector:
        return HTTPConfigCenterConnector(
            settings.require_config_center_config(), credentials, transport=transport
        )

    return ConnectorFactory[ConfigCenterConnector](
        "config_center", fake=FakeConfigCenterConnector, real=real
    ).create(settings)
