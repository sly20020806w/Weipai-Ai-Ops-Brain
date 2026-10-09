"""两个 L0 图查询 Tool；不联网补查，freshness 在读取时按 UTC 计算。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import or_, select

from app.connectors.ops_platform.models import Identifier
from app.db.base import utc_now
from app.graph.models import GraphEdge, GraphNode
from app.graph.service import GraphNodeNotFound, GraphService
from app.policy.models import RiskLevel
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class ContextQuery(ToolModel):
    service_name: Identifier
    hops: int = Field(default=2, ge=1, le=4)


class DependencyQuery(ToolModel):
    service_name: Identifier
    hops: int = Field(default=1, ge=1, le=4)
    direction: Literal["upstream", "downstream", "both"] = "both"


class NodeView(ToolModel):
    id: UUID
    kind: str
    external_id: str
    name: str


class EdgeView(ToolModel):
    id: UUID
    from_node_id: UUID
    to_node_id: UUID
    relation: str
    source: str
    confidence: float
    first_seen: datetime
    last_seen: datetime
    freshness_seconds: float


class GraphContext(ToolModel):
    service_name: Identifier
    as_of: datetime
    nodes: tuple[NodeView, ...]
    edges: tuple[EdgeView, ...]


class GraphQueries:
    def __init__(self, graph: GraphService) -> None:
        self.graph = graph

    async def read(
        self, service_name: str, hops: int, direction: str = "both", *, dependencies: bool = False
    ) -> GraphContext:
        session = self.graph.session
        root = await session.scalar(
            select(GraphNode).where(
                GraphNode.kind == "service", GraphNode.external_id == service_name
            )
        )
        if root is None:
            raise GraphNodeNotFound("服务尚未发现")
        as_of = utc_now()
        if dependencies:
            # 上下游仅沿 ARMS 实际 calls 边遍历，不能通过共同资源/负责人推断依赖。
            ids = {root.id}
            frontier = {root.id}
            for _ in range(hops):
                edges = list(
                    await session.scalars(
                        select(GraphEdge).where(
                            GraphEdge.relation == "calls",
                            or_(
                                GraphEdge.from_node_id.in_(frontier),
                                GraphEdge.to_node_id.in_(frontier),
                            ),
                        )
                    )
                )
                found = set()
                for edge in edges:
                    if direction in {"downstream", "both"} and edge.from_node_id in frontier:
                        found.add(edge.to_node_id)
                    if direction in {"upstream", "both"} and edge.to_node_id in frontier:
                        found.add(edge.from_node_id)
                frontier = found - ids
                ids |= frontier
                if not frontier:
                    break
            nodes = list(await session.scalars(select(GraphNode).where(GraphNode.id.in_(ids))))
            edges = list(
                await session.scalars(
                    select(GraphEdge).where(
                        GraphEdge.relation == "calls",
                        GraphEdge.from_node_id.in_(ids),
                        GraphEdge.to_node_id.in_(ids),
                    )
                )
            )
        else:
            nodes = [root, *await self.graph.neighbors(root.id, hops=hops, direction="both")]
            ids = {value.id for value in nodes}
            edges = list(
                await session.scalars(
                    select(GraphEdge).where(
                        GraphEdge.from_node_id.in_(ids), GraphEdge.to_node_id.in_(ids)
                    )
                )
            )
        return GraphContext(
            service_name=service_name,
            as_of=as_of,
            nodes=tuple(
                NodeView(id=n.id, kind=n.kind, external_id=n.external_id, name=n.name)
                for n in sorted(nodes, key=lambda n: (n.kind, n.external_id))
            ),
            edges=tuple(
                EdgeView(
                    id=e.id,
                    from_node_id=e.from_node_id,
                    to_node_id=e.to_node_id,
                    relation=e.relation,
                    source=e.source,
                    confidence=e.confidence,
                    first_seen=e.first_seen,
                    last_seen=e.last_seen,
                    freshness_seconds=e.freshness_at(as_of).total_seconds(),
                )
                for e in sorted(edges, key=lambda e: str(e.id))
                if e.from_node_id in ids and e.to_node_id in ids
            ),
        )


def register_graph_tools(registry: ToolRegistry, graph: GraphService) -> None:
    queries = GraphQueries(graph)

    async def context(query: ContextQuery) -> GraphContext:
        return await queries.read(query.service_name, query.hops)

    async def dependencies(query: DependencyQuery) -> GraphContext:
        return await queries.read(
            query.service_name, query.hops, query.direction, dependencies=True
        )

    registry.register(
        name="get_service_context",
        description="读取服务认知图及来源/置信度/新鲜度",
        input_model=ContextQuery,
        output_model=GraphContext,
        handler=context,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="get_dependencies",
        description="读取实际调用证据发现的上下游关系",
        input_model=DependencyQuery,
        output_model=GraphContext,
        handler=dependencies,
        risk_level=RiskLevel.L0,
    )
