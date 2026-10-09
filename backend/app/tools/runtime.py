"""调查 Tool 的组合根；Agent 不接触 Connector、SDK 或实现函数。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.changes.factory import (
    create_argocd_connector,
    create_ci_connector,
    create_config_center_connector,
    create_git_connector,
)
from app.connectors.cloud.factory import create_cloud_connector
from app.connectors.kubernetes.factory import create_kubernetes_connector
from app.connectors.observability.factory import (
    create_arms_connector,
    create_prometheus_connector,
    create_sls_connector,
)
from app.graph.changes.service import TimelineService
from app.graph.service import GraphService
from app.learning.service import IncidentService
from app.runbooks.embedding import embedding_client
from app.runbooks.service import RunbookService
from app.tools.changes import register_change_tools
from app.tools.cloud import register_cloud_tools
from app.tools.graph import register_graph_tools
from app.tools.incidents import register_incident_tools
from app.tools.kubernetes import register_kubernetes_tools
from app.tools.observability import register_observability_tools
from app.tools.registry import ToolRegistry
from app.tools.runbooks import register_runbook_tools
from app.tools.timeline import register_timeline_tools


@asynccontextmanager
async def investigation_registry(
    settings: Settings, session: AsyncSession, *, reviewer: bool = False
) -> AsyncIterator[ToolRegistry]:
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        register_incident_tools(registry, IncidentService(session))
        register_runbook_tools(
            registry, RunbookService(session, lambda request: embedding_client(settings, request))
        )
        register_graph_tools(registry, GraphService(session))
        register_timeline_tools(registry, TimelineService(session))
        prometheus = await stack.enter_async_context(create_prometheus_connector(settings))
        sls = await stack.enter_async_context(create_sls_connector(settings))
        arms = await stack.enter_async_context(create_arms_connector(settings, reviewer=reviewer))
        register_observability_tools(registry, prometheus, sls, arms)
        if settings.agent_config.experts_enabled:
            kubernetes = await stack.enter_async_context(create_kubernetes_connector(settings))
            cloud = await stack.enter_async_context(create_cloud_connector(settings))
            git = await stack.enter_async_context(create_git_connector(settings))
            ci = await stack.enter_async_context(create_ci_connector(settings))
            release = await stack.enter_async_context(create_argocd_connector(settings))
            config_center = await stack.enter_async_context(
                create_config_center_connector(settings)
            )
            register_kubernetes_tools(registry, kubernetes)
            register_cloud_tools(registry, cloud)
            register_change_tools(registry, git, ci, release, config_center)
        yield registry
