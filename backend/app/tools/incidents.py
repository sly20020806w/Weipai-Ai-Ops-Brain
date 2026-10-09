"""L0 历史事故检索；查询、证据、审计和 Replay 均经唯一 Dispatcher。"""

from app.learning.models import IncidentHit, IncidentSearch
from app.learning.service import IncidentService
from app.policy.models import RiskLevel
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class SearchIncidentsOutput(ToolModel):
    matches: tuple[IncidentHit, ...]


def register_incident_tools(registry: ToolRegistry, service: IncidentService) -> None:
    async def search(query: IncidentSearch) -> SearchIncidentsOutput:
        return SearchIncidentsOutput(matches=await service.search(query))

    registry.register(
        name="search_incidents",
        description="检索带 Evidence 引用的历史事故复盘与改进任务",
        input_model=IncidentSearch,
        output_model=SearchIncidentsOutput,
        handler=search,
        risk_level=RiskLevel.L0,
    )
