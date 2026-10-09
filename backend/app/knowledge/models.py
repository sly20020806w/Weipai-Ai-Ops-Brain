"""PostgreSQL 知识条目和 pgvector；不存环境配置或凭证。"""

from datetime import datetime

from pgvector.sqlalchemy import VECTOR  # type: ignore[import-untyped]  # 官方适配器未带类型声明。
from sqlalchemy import CheckConstraint, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime
from app.knowledge.schemas import KnowledgeType


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind.value}'" for kind in KnowledgeType) + ")",
            name="knowledge_type",
        ),
        CheckConstraint("length(trim(content)) > 0", name="content_not_blank"),
        CheckConstraint("length(content) <= 20000", name="content_length"),
        CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(trim(embedding_model)) > 0", name="model_not_blank"),
        CheckConstraint("expires_at IS NULL OR expires_at > valid_from", name="validity_order"),
        CheckConstraint(
            "embedding_dimensions BETWEEN 1 AND 16000 AND "
            "vector_dims(embedding) = embedding_dimensions",
            name="vector_dimensions",
        ),
        CheckConstraint("vector_norm(embedding) > 0", name="vector_not_zero"),
        Index("ix_knowledge_entries_embedding_space", "embedding_model", "embedding_dimensions"),
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    embedding: Mapped[list[float]] = mapped_column(VECTOR(), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(256), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
