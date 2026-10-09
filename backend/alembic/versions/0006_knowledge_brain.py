"""知识条目与 pgvector 嵌入。"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR  # type: ignore[import-untyped]  # 官方适配器未带类型声明。

from alembic import op

revision: str = "0006_knowledge_brain"
down_revision: str | Sequence[str] | None = "0005_change_timeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.Column("embedding_model", sa.String(256), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('business_rule', 'standard', 'sop', 'experience', 'constraint', "
            "'team_convention', 'business_priority')",
            name="knowledge_type",
        ),
        sa.CheckConstraint("length(trim(content)) > 0", name="content_not_blank"),
        sa.CheckConstraint("length(content) <= 20000", name="content_length"),
        sa.CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("length(trim(embedding_model)) > 0", name="model_not_blank"),
        sa.CheckConstraint("expires_at IS NULL OR expires_at > valid_from", name="validity_order"),
        sa.CheckConstraint(
            "embedding_dimensions BETWEEN 1 AND 16000 AND "
            "vector_dims(embedding) = embedding_dimensions",
            name="vector_dimensions",
        ),
        sa.CheckConstraint("vector_norm(embedding) > 0", name="vector_not_zero"),
    )
    op.create_index(
        "ix_knowledge_entries_embedding_space",
        "knowledge_entries",
        ["embedding_model", "embedding_dimensions"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_entries_embedding_space", table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
    # 扩展可能被其他表共用，不在本步骤降级时删除。
