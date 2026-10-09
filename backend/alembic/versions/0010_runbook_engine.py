"""Runbook 全部内容字段、成熟度与向量。"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR  # type: ignore[import-untyped]
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_runbook_engine"
down_revision: str | Sequence[str] | None = "0009_state_prediction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "runbooks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("applicability_conditions", postgresql.JSONB(), nullable=False),
        sa.Column("exclusion_conditions", postgresql.JSONB(), nullable=False),
        sa.Column("diagnostic_steps", postgresql.JSONB(), nullable=False),
        sa.Column("handling_steps", postgresql.JSONB(), nullable=False),
        sa.Column("risk_level", sa.String(2), nullable=False),
        sa.Column("rollback_plan", sa.Text(), nullable=False),
        sa.Column("verification_steps", postgresql.JSONB(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("automation_level", sa.String(32), nullable=False),
        sa.Column("maturity", sa.String(32), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.Column("embedding_model", sa.String(256), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.CheckConstraint("name ~ '^[a-z][a-z0-9_-]{0,127}$'", name="name_format"),
        sa.CheckConstraint("length(trim(description)) > 0", name="description_not_blank"),
        sa.CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(trim(rollback_plan)) > 0", name="rollback_not_blank"),
        sa.CheckConstraint("risk_level IN ('L0','L1','L2','L3','L4','L5')", name="risk_level"),
        sa.CheckConstraint("success_count >= 0 AND failure_count >= 0", name="counts_nonnegative"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        sa.CheckConstraint(
            "maturity IN ('draft','reviewed','verified','semi_automated',"
            "'approval_automated','self_healing')",
            name="maturity_values",
        ),
        sa.CheckConstraint(
            "automation_level IN ('manual','semi_automated','approval_automated','self_healing')",
            name="automation_values",
        ),
        *(
            sa.CheckConstraint(
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
        sa.CheckConstraint("length(trim(embedding_model)) > 0", name="model_not_blank"),
        sa.CheckConstraint(
            "embedding_dimensions BETWEEN 1 AND 16000 AND "
            "vector_dims(embedding) = embedding_dimensions",
            name="vector_dimensions",
        ),
        sa.CheckConstraint("vector_norm(embedding) > 0", name="vector_not_zero"),
    )
    op.create_index(
        "ix_runbooks_embedding_space", "runbooks", ["embedding_model", "embedding_dimensions"]
    )


def downgrade() -> None:
    op.drop_index("ix_runbooks_embedding_space", table_name="runbooks")
    op.drop_table("runbooks")
