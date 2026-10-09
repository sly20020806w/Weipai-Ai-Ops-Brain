"""OpsEvent 接纳 Temporal 定时来源，复用既有任务与去重表。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_scheduled_events"
down_revision: str | Sequence[str] | None = "0007_ops_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def origin_constraint(*, include_schedule: bool) -> None:
    origins = (
        "'prometheus','kubernetes','ops_platform','git','ci','argocd',"
        "'config_center','cloud','manual'"
    )
    if include_schedule:
        origins += ",'schedule'"
    op.create_check_constraint("origin_valid", "ops_events", f"origin IN ({origins})")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ops_events_origin_valid"), "ops_events", type_="check")
    origin_constraint(include_schedule=True)


def downgrade() -> None:
    # 已有定时证据不删除、不伪造来源；非空时数据库会拒绝不兼容降级。
    op.drop_constraint(op.f("ck_ops_events_origin_valid"), "ops_events", type_="check")
    origin_constraint(include_schedule=False)
