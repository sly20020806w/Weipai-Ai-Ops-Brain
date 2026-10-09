"""允许学习生成 OpsEvent；已有改进记录时拒绝有损降级。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_postmortem"
down_revision: str | Sequence[str] | None = "0011_human_interaction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def origins(include_learning: bool) -> None:
    values = (
        "'prometheus','kubernetes','ops_platform','git','ci','argocd',"
        "'config_center','cloud','manual','schedule','state','prediction'"
    )
    if include_learning:
        values += ",'learning'"
    op.create_check_constraint("origin_valid", "ops_events", f"origin IN ({values})")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_ops_events_origin_valid"), "ops_events", type_="check")
    origins(True)


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM ops_events WHERE origin='learning'")):
        raise ValueError("已有复盘改进事件，不能无损降级；请保留当前版本")
    op.drop_constraint(op.f("ck_ops_events_origin_valid"), "ops_events", type_="check")
    origins(False)
