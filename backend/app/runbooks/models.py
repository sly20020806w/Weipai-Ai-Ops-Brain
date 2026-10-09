"""Runbook、条件与步骤快照存 PostgreSQL，语义检索使用 pgvector。"""

from pgvector.sqlalchemy import VECTOR  # type: ignore[import-untyped]
from sqlalchemy import CheckConstraint, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.runbooks.schemas import AutomationLevel, RunbookMaturity
from app.tools.models import JsonObject


class Runbook(Base):
    __tablename__ = "runbooks"
    __table_args__ = (
        CheckConstraint("name ~ '^[a-z][a-z0-9_-]{0,127}$'", name="name_format"),
        CheckConstraint("length(trim(description)) > 0", name="description_not_blank"),
        CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(trim(rollback_plan)) > 0", name="rollback_not_blank"),
        CheckConstraint("risk_level IN ('L0','L1','L2','L3','L4','L5')", name="risk_level"),
        CheckConstraint("success_count >= 0 AND failure_count >= 0", name="counts_nonnegative"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint("content_version >= 1", name="content_version_positive"),
        CheckConstraint(
            "maturity IN (" + ",".join(f"'{item.value}'" for item in RunbookMaturity) + ")",
            name="maturity_values",
        ),
        CheckConstraint(
            "automation_level IN (" + ",".join(f"'{item.value}'" for item in AutomationLevel) + ")",
            name="automation_values",
        ),
        *(
            CheckConstraint(
                f"jsonb_typeof({name}) = 'array' AND "
                f"jsonb_array_length({name}) BETWEEN {minimum} AND 30",
                name=f"{name}_array",
            )
            for name, minimum in (
                ("applicability_conditions", 1),
                ("exclusion_conditions", 0),
                ("diagnostic_steps", 1),
                ("handling_steps", 1),
                ("verification_steps", 1),
            )
        ),
        CheckConstraint("length(trim(embedding_model)) > 0", name="model_not_blank"),
        CheckConstraint(
            "embedding_dimensions BETWEEN 1 AND 16000 AND "
            "vector_dims(embedding) = embedding_dimensions",
            name="vector_dimensions",
        ),
        CheckConstraint("vector_norm(embedding) > 0", name="vector_not_zero"),
        Index("ix_runbooks_embedding_space", "embedding_model", "embedding_dimensions"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    applicability_conditions: Mapped[list[JsonObject]] = mapped_column(JSONB, nullable=False)
    exclusion_conditions: Mapped[list[JsonObject]] = mapped_column(JSONB, nullable=False)
    diagnostic_steps: Mapped[list[JsonObject]] = mapped_column(JSONB, nullable=False)
    handling_steps: Mapped[list[JsonObject]] = mapped_column(JSONB, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(2), nullable=False)
    rollback_plan: Mapped[str] = mapped_column(Text, nullable=False)
    verification_steps: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    automation_level: Mapped[str] = mapped_column(String(32), nullable=False)
    maturity: Mapped[str] = mapped_column(String(32), nullable=False)
    content_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    embedding: Mapped[list[float]] = mapped_column(VECTOR(), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(256), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
