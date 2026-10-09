"""持久化 Runbook 内容版本，修改内容后旧审核和验证记录失效。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_runbook_maturity"
down_revision: str | Sequence[str] | None = "0012_postmortem"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runbooks", sa.Column("content_version", sa.Integer(), server_default="1", nullable=False)
    )
    op.create_check_constraint("content_version_positive", "runbooks", "content_version >= 1")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM evidence_ledger WHERE source_tool='runbook.lifecycle'")
    ):
        raise ValueError("已有 Runbook 成熟度证据，不能丢失内容版本；请保留当前版本")
    op.drop_constraint(op.f("ck_runbooks_content_version_positive"), "runbooks", type_="check")
    op.drop_column("runbooks", "content_version")
