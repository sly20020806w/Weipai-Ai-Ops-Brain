"""只读验证 Tool 组合根，生产来源复用既有 Connector 工厂；Fake 恢复需显式注入。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.cloud.factory import create_cloud_connector
from app.connectors.cloud.fake import FakeCloudConnector, sample_resources
from app.connectors.cloud.models import RDSConnections
from app.connectors.kubernetes.factory import create_kubernetes_connector
from app.connectors.kubernetes.fake import FakeKubernetesConnector
from app.connectors.kubernetes.verification_fake import verification_snapshot
from app.connectors.observability.factory import (
    create_arms_connector,
    create_prometheus_connector,
    create_sls_connector,
)
from app.connectors.observability.fake import (
    SAMPLE_END,
    SAMPLE_START,
    FakeARMSConnector,
    FakePrometheusConnector,
    FakeSLSConnector,
)
from app.connectors.observability.verification_fake import (
    verification_logs,
    verification_metrics,
    verification_traces,
)
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tools.cloud import register_cloud_tools
from app.tools.dispatcher import ToolDispatcher
from app.tools.kubernetes import register_kubernetes_tools
from app.tools.observability import register_observability_tools
from app.tools.registry import ToolRegistry
from app.tools.verification import register_verification_tool
from app.verifier.engine import VerificationEngine


def add_verification(
    registry: ToolRegistry, settings: Settings, session: AsyncSession
) -> ToolDispatcher:
    dispatcher = ToolDispatcher(registry, create_policy_engine(settings), LedgerService(session))
    register_verification_tool(
        registry, VerificationEngine(dispatcher, settings.verification_config)
    )
    return dispatcher


@asynccontextmanager
async def verification_registry(
    settings: Settings, session: AsyncSession
) -> AsyncIterator[ToolRegistry]:
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        kubernetes = await stack.enter_async_context(create_kubernetes_connector(settings))
        cloud = await stack.enter_async_context(create_cloud_connector(settings))
        prometheus = await stack.enter_async_context(create_prometheus_connector(settings))
        sls = await stack.enter_async_context(create_sls_connector(settings))
        arms = await stack.enter_async_context(create_arms_connector(settings))
        register_kubernetes_tools(registry, kubernetes)
        register_cloud_tools(registry, cloud)
        register_observability_tools(registry, prometheus, sls, arms)
        add_verification(registry, settings, session)
        yield registry


@asynccontextmanager
async def fake_verification_registry(
    settings: Settings,
    session: AsyncSession,
    *,
    recovered: bool = True,
    window_start: datetime = SAMPLE_START,
    window_end: datetime = SAMPLE_END,
) -> AsyncIterator[ToolRegistry]:
    if settings.app_env not in {"local", "test"} or settings.connector_mode.value != "fake":
        raise ValueError("验证恢复样例只允许 local/test + Fake")
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        kubernetes = await stack.enter_async_context(
            FakeKubernetesConnector(verification_snapshot(recovered=recovered))
        )
        resource = next(r for r in sample_resources() if r.product == "rds")
        resource = resource.model_copy(
            update={
                "rds_connections": RDSConnections(
                    availability="available",
                    max_connections=600,
                    sampled_at=window_end - timedelta(seconds=60),
                    active_connections=60.0,
                    total_connections=80.0 if recovered else 580.0,
                )
            }
        )
        cloud = await stack.enter_async_context(
            FakeCloudConnector(resources=(resource,), events=())
        )
        prometheus = await stack.enter_async_context(
            FakePrometheusConnector(
                verification_metrics(window_start, window_end, recovered=recovered)
            )
        )
        sls = await stack.enter_async_context(
            FakeSLSConnector(verification_logs(window_start, recovered=recovered))
        )
        arms = await stack.enter_async_context(
            FakeARMSConnector(verification_traces(window_start, recovered=recovered))
        )
        register_kubernetes_tools(registry, kubernetes)
        register_cloud_tools(registry, cloud)
        register_observability_tools(registry, prometheus, sls, arms)
        add_verification(registry, settings, session)
        yield registry
