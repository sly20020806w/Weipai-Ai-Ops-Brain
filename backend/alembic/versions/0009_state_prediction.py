"""检测游标只引用 OpsEvent，扩展 State/Prediction 事件来源。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_state_prediction"
down_revision: str | Sequence[str] | None = "0008_scheduled_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def origins(include_detection: bool) -> None:
    values = (
        "'prometheus','kubernetes','ops_platform','git','ci','argocd',"
        "'config_center','cloud','manual','schedule'"
    )
    if include_detection:
        values += ",'state','prediction'"
    op.create_check_constraint("origin_valid", "ops_events", f"origin IN ({values})")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ops_events_origin_valid"), "ops_events", type_="check")
    origins(True)
    op.create_table(
        "detection_cursors",
        sa.Column("detector_key", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_event_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("detector_key"),
        sa.ForeignKeyConstraint(["active_event_id"], ["ops_events.id"]),
        sa.CheckConstraint("detector_key ~ '^[0-9a-f]{64}$'", name="detector_key_format"),
    )


def downgrade() -> None:
    # 有检测事件时不删除历史证据；数据库拒绝不兼容降级。
    op.drop_constraint(op.f("ck_ops_events_origin_valid"), "ops_events", type_="check")
    origins(False)
    op.drop_table("detection_cursors")
