"""重大保障只读 Tool 组合；外部事实仅由 Connector 获得。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.kubernetes.execution import KubernetesWriteConnector
from app.connectors.war_room.facts import WarRoomConnector, WarRoomFacts, WarRoomQuery
from app.policy.models import RiskLevel
from app.tools.architecture import architecture_registry
from app.tools.execution import register_execution_tools
from app.tools.registry import ToolRegistry


def war_room_registry(
    session: AsyncSession,
    settings: Settings,
    facts: WarRoomConnector,
    resources: KubernetesWriteConnector,
) -> ToolRegistry:
    registry = architecture_registry(session, settings)
    register_execution_tools(registry, resources, settings.execution_config.credential_ttl_seconds)
    registry.register(
        name="query_war_room_facts",
        description="读取保障容量、巡检风险及监控窗口事实",
        input_model=WarRoomQuery,
        output_model=WarRoomFacts,
        handler=facts.query,
        risk_level=RiskLevel.L0,
    )
    return registry
