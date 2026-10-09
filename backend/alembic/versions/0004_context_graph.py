"""建立 Context Graph 节点与带来源、置信度和观察时间的有向边。

Revision ID: 0004_context_graph
Revises: 0003_evidence_ledger
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from alembic import op

revision: str = "0004_context_graph"
down_revision: str | Sequence[str] | None = "0003_evidence_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common_columns() -> list[sa.Column[UUID] | sa.Column[datetime]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "context_graph_nodes",
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("external_id", sa.String(300), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        *_common_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "external_id"),
        sa.CheckConstraint("length(trim(kind)) > 0", name="kind_not_blank"),
        sa.CheckConstraint("length(trim(external_id)) > 0", name="external_id_not_blank"),
        sa.CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )
    op.create_table(
        "context_graph_edges",
        sa.Column("from_node_id", sa.Uuid(), nullable=False),
        sa.Column("to_node_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(100), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        *_common_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["from_node_id"], ["context_graph_nodes.id"]),
        sa.ForeignKeyConstraint(["to_node_id"], ["context_graph_nodes.id"]),
        sa.UniqueConstraint("from_node_id", "to_node_id", "relation", "source"),
        sa.CheckConstraint("length(trim(relation)) > 0", name="relation_not_blank"),
        sa.CheckConstraint("length(trim(source)) > 0", name="source_not_blank"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        sa.CheckConstraint("last_seen >= first_seen", name="observation_order"),
    )
    op.create_index("ix_context_graph_edges_from_node_id", "context_graph_edges", ["from_node_id"])
    op.create_index("ix_context_graph_edges_to_node_id", "context_graph_edges", ["to_node_id"])


def downgrade() -> None:
    op.drop_index("ix_context_graph_edges_to_node_id", table_name="context_graph_edges")
    op.drop_index("ix_context_graph_edges_from_node_id", table_name="context_graph_edges")
    op.drop_table("context_graph_edges")
    op.drop_table("context_graph_nodes")
