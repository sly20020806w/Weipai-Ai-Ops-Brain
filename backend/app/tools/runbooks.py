"""Runbook 检索为 L0，所有调用与历史回放复用唯一 Dispatcher。"""

from app.policy.models import RiskLevel
from app.runbooks.schemas import RunbookHit, RunbookSearch
from app.runbooks.service import RunbookService
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class SearchRunbooksOutput(ToolModel):
    matches: tuple[RunbookHit, ...]


def register_runbook_tools(registry: ToolRegistry, service: RunbookService) -> None:
    async def search(query: RunbookSearch) -> SearchRunbooksOutput:
        return SearchRunbooksOutput(matches=await service.search(query))

    registry.register(
        name="search_runbooks",
        description="语义检索 Runbook；必须另行验证适用和排除条件",
        input_model=RunbookSearch,
        output_model=SearchRunbooksOutput,
        handler=search,
        risk_level=RiskLevel.L0,
    )
