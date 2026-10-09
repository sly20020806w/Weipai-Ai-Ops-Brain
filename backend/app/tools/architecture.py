"""架构评审仅取得四个高级只读 Tool，不打开外部运维 Connector。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.graph.service import GraphService
from app.learning.service import IncidentService
from app.runbooks.embedding import embedding_client
from app.runbooks.service import RunbookService
from app.tools.graph import register_graph_tools
from app.tools.incidents import register_incident_tools
from app.tools.knowledge import register_knowledge_tools
from app.tools.registry import ToolRegistry
from app.tools.runbooks import register_runbook_tools

ALLOWED_TOOLS = frozenset(
    {"search_runbooks", "get_service_context", "search_knowledge", "search_incidents"}
)


def architecture_registry(session: AsyncSession, settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    register_runbook_tools(
        registry, RunbookService(session, lambda request: embedding_client(settings, request))
    )
    register_graph_tools(registry, GraphService(session))
    register_knowledge_tools(registry, session, settings)
    register_incident_tools(registry, IncidentService(session))
    return registry
