"""复用 ConnectorFactory；默认 Fake，local/test 不能创建真实客户端。"""

import httpx2 as httpx

from app.config import Settings
from app.connectors.factory import ConnectorFactory
from app.connectors.models import ReaderCredentials
from app.connectors.observability.arms import HTTPARMSConnector
from app.connectors.observability.base import ARMSConnector, PrometheusConnector, SLSConnector
from app.connectors.observability.fake import (
    FakeARMSConnector,
    FakePrometheusConnector,
    FakeSLSConnector,
)
from app.connectors.observability.prometheus import HTTPPrometheusConnector
from app.connectors.observability.reviewer_fake import reviewer_traces
from app.connectors.observability.sls import HTTPSLSConnector


def create_prometheus_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> PrometheusConnector:
    def real(credentials: ReaderCredentials) -> PrometheusConnector:
        return HTTPPrometheusConnector(
            settings.require_prometheus_config(), credentials, transport=transport
        )

    return ConnectorFactory[PrometheusConnector](
        "prometheus", fake=FakePrometheusConnector, real=real
    ).create(settings)


def create_sls_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None
) -> SLSConnector:
    def real(credentials: ReaderCredentials) -> SLSConnector:
        return HTTPSLSConnector(settings.require_sls_config(), credentials, transport=transport)

    return ConnectorFactory[SLSConnector]("sls", fake=FakeSLSConnector, real=real).create(settings)


def create_arms_connector(
    settings: Settings, *, transport: httpx.MockTransport | None = None, reviewer: bool = False
) -> ARMSConnector:
    def real(credentials: ReaderCredentials) -> ARMSConnector:
        return HTTPARMSConnector(settings.require_arms_config(), credentials, transport=transport)

    return ConnectorFactory[ARMSConnector](
        "arms",
        fake=lambda: FakeARMSConnector(reviewer_traces()) if reviewer else FakeARMSConnector(),
        real=real,
    ).create(settings)
