"""图存储服务：事务由调用方提交，关系观察以 PostgreSQL 原子 upsert 落库。"""

import math
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import UTCDateTime, utc_now
from app.graph.models import GraphEdge, GraphNode


class GraphNodeNotFound(LookupError):
    pass


def _required_text(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field} 必须非空且符合长度限制")
    return value.strip()


class GraphService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _require_transaction(self) -> None:
        if not self.session.in_transaction():
            raise RuntimeError("调用 graph 服务前请使用 async with session.begin() 开启事务")

    async def upsert_node(self, *, kind: str, external_id: str, name: str) -> GraphNode:
        self._require_transaction()
        statement = insert(GraphNode).values(
            id=uuid4(),
            kind=_required_text(kind, "kind", 100),
            external_id=_required_text(external_id, "external_id", 300),
            name=_required_text(name, "name", 300),
        )
        upsert = statement.on_conflict_do_update(
            index_elements=[GraphNode.kind, GraphNode.external_id],
            set_={"name": statement.excluded.name, "updated_at": utc_now()},
        ).returning(GraphNode)
        result = await self.session.scalars(upsert.execution_options(populate_existing=True))
        return result.one()

    async def get_node(self, node_id: UUID) -> GraphNode:
        node = await self.session.get(GraphNode, node_id)
        if node is None:
            raise GraphNodeNotFound(f"图节点不存在：{node_id}")
        return node

    async def upsert_edge(
        self,
        *,
        from_node_id: UUID,
        to_node_id: UUID,
        relation: str,
        source: str,
        confidence: float,
        observed_at: datetime | None = None,
    ) -> GraphEdge:
        self._require_transaction()
        relation = _required_text(relation, "relation", 100)
        source = _required_text(source, "source", 200)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("confidence 必须是 0–1 之间的有限数值")
        observed_at = UTCDateTime.normalize(observed_at) or utc_now()
        statement = insert(GraphEdge).values(
            id=uuid4(),
            from_node_id=from_node_id,
            to_node_id=to_node_id,
            relation=relation,
            source=source,
            confidence=confidence,
            first_seen=observed_at,
            last_seen=observed_at,
        )
        upsert = statement.on_conflict_do_update(
            index_elements=[
                GraphEdge.from_node_id,
                GraphEdge.to_node_id,
                GraphEdge.relation,
                GraphEdge.source,
            ],
            # 同来源同边只刷新 last_seen；乱序观察不会倒退时间或覆盖置信度。
            set_={"last_seen": func.greatest(GraphEdge.last_seen, statement.excluded.last_seen)},
        ).returning(GraphEdge)
        result = await self.session.scalars(upsert.execution_options(populate_existing=True))
        return result.one()

    async def neighbors(
        self,
        node_id: UUID,
        *,
        hops: int,
        direction: Literal["outgoing", "incoming", "both"] = "outgoing",
    ) -> list[GraphNode]:
        """返回 1..N 跳内的唯一节点，不含起点，默认沿有向边向外查询。"""
        if isinstance(hops, bool) or not isinstance(hops, int) or hops < 0:
            raise ValueError("hops 必须为非负整数")
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError("direction 必须为 outgoing、incoming 或 both")
        await self.get_node(node_id)
        if hops == 0:
            return []
        outgoing = select(
            GraphEdge.from_node_id.label("start_id"), GraphEdge.to_node_id.label("end_id")
        )
        incoming = select(
            GraphEdge.to_node_id.label("start_id"), GraphEdge.from_node_id.label("end_id")
        )
        arcs = (
            outgoing.union(incoming)
            if direction == "both"
            else outgoing
            if direction == "outgoing"
            else incoming
        ).subquery("arcs")
        reached = (
            select(GraphNode.id.label("node_id"), literal(0).label("depth"))
            .where(GraphNode.id == node_id)
            .cte("reached", recursive=True)
        )
        reached = reached.union(
            select(arcs.c.end_id, reached.c.depth + 1)
            .join(reached, arcs.c.start_id == reached.c.node_id)
            .where(reached.c.depth < hops)
        )
        # UNION 按节点/深度去重，避免菱形路径爆炸；深度上限保证环路终止。
        result = await self.session.scalars(
            select(GraphNode)
            .where(GraphNode.id.in_(select(reached.c.node_id)), GraphNode.id != node_id)
            .order_by(GraphNode.kind, GraphNode.external_id, GraphNode.id)
        )
        return list(result)
