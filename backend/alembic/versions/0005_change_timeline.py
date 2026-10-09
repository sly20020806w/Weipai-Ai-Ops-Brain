"""新增最小变更事件引用表和原子去重约束。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_change_timeline"
down_revision: str | Sequence[str] | None = "0004_context_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("service_name", sa.String(128), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(512), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.String(512), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_name", "source", "kind", "source_ref"),
        sa.CheckConstraint("length(trim(service_name)) > 0", name="service_not_blank"),
        sa.CheckConstraint("length(trim(source_ref)) > 0", name="reference_not_blank"),
        sa.CheckConstraint(
            "(source IN ('gitlab', 'github') AND kind IN ('Commit', 'Merge')) OR "
            "(source IN ('jenkins', 'gitlab_ci') AND kind = 'Build') OR "
            "(source = 'argocd' AND kind = 'Sync') OR "
            "(source = 'config_center' AND kind = 'Config') OR "
            "(source = 'kubernetes' AND kind IN ('Image', 'Deploy', 'KubernetesEvent')) OR "
            "(source = 'alibaba_cloud' AND kind = 'CloudEvent')",
            name="source_kind",
        ),
    )
    op.create_index(
        "ix_change_events_service_time", "change_events", ["service_name", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_change_events_service_time", table_name="change_events")
    op.drop_table("change_events")
