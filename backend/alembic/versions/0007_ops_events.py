"""统一运维事件与任务的唯一关联。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_ops_events"
down_revision: str | Sequence[str] | None = "0006_knowledge_brain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ops_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("service_name", sa.String(256), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["ai_tasks.id"]),
        sa.UniqueConstraint("fingerprint"),
        sa.UniqueConstraint("task_id"),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_format"),
        sa.CheckConstraint(
            "source IN ('Alert','Ticket','Schedule','State','Prediction','Release','Human','AI')",
            name="source_valid",
        ),
        sa.CheckConstraint(
            "length(trim(external_id)) > 0 AND length(trim(service_name)) > 0 "
            "AND length(trim(title)) > 0",
            name="required_text",
        ),
        sa.CheckConstraint(
            "origin IN ('prometheus','kubernetes','ops_platform','git','ci','argocd',"
            "'config_center','cloud','manual')",
            name="origin_valid",
        ),
    )
    op.create_index("ix_ops_events_service_time", "ops_events", ["service_name", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_ops_events_service_time", table_name="ops_events")
    op.drop_table("ops_events")
