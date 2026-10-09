"""Context Graph 的关系存储；原始运维数据留在源系统。"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, utc_now


class GraphNode(Base):
    __tablename__ = "context_graph_nodes"
    __table_args__ = (
        UniqueConstraint("kind", "external_id"),
        CheckConstraint("length(trim(kind)) > 0", name="kind_not_blank"),
        CheckConstraint("length(trim(external_id)) > 0", name="external_id_not_blank"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )

    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    external_id: Mapped[str] = mapped_column(String(300), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)


class GraphEdge(Base):
    __tablename__ = "context_graph_edges"
    __table_args__ = (
        UniqueConstraint("from_node_id", "to_node_id", "relation", "source"),
        CheckConstraint("length(trim(relation)) > 0", name="relation_not_blank"),
        CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("last_seen >= first_seen", name="observation_order"),
        Index("ix_context_graph_edges_from_node_id", "from_node_id"),
        Index("ix_context_graph_edges_to_node_id", "to_node_id"),
    )

    from_node_id: Mapped[UUID] = mapped_column(ForeignKey("context_graph_nodes.id"), nullable=False)
    to_node_id: Mapped[UUID] = mapped_column(ForeignKey("context_graph_nodes.id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    def freshness_at(self, now: datetime) -> timedelta:
        """距最近一次观察的时间；未来的观察时间按零计算。"""
        normalized = UTCDateTime.normalize(now)
        assert normalized is not None
        last_seen = UTCDateTime.normalize(self.last_seen)
        assert last_seen is not None
        return max(normalized - last_seen, timedelta(0))

    @property
    def freshness(self) -> timedelta:
        return self.freshness_at(utc_now())
