"""架构评审的 L0 公司规范检索；向量仍仅由公司网关或显式 Fake 提供。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import EmbeddingRequest
from app.config import Settings
from app.knowledge.schemas import KnowledgeMatch, KnowledgeSearch
from app.knowledge.service import KnowledgeService
from app.policy.models import RiskLevel
from app.runbooks.embedding import embedding_client
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class SearchKnowledgeOutput(ToolModel):
    matches: tuple[KnowledgeMatch, ...]


class SearchKnowledgeQuery(KnowledgeSearch, ToolModel):
    """复用知识查询字段，同时满足统一 Tool 的严格 schema 基类。"""


def register_knowledge_tools(
    registry: ToolRegistry, session: AsyncSession, settings: Settings
) -> None:
    async def search(query: SearchKnowledgeQuery) -> SearchKnowledgeOutput:
        llm = embedding_client(settings, EmbeddingRequest(inputs=(query.query,)))
        try:
            return SearchKnowledgeOutput(matches=await KnowledgeService(session, llm).search(query))
        finally:
            await llm.aclose()

    registry.register(
        name="search_knowledge",
        description="按 UTC 有效期检索公司规范知识及来源",
        input_model=SearchKnowledgeQuery,
        output_model=SearchKnowledgeOutput,
        handler=search,
        risk_level=RiskLevel.L0,
    )
