"""巡检风险只保存分类、观察时间和 Evidence 引用。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_inspection_risks"
down_revision: str | Sequence[str] | None = "0013_runbook_maturity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inspection_risks",
        sa.Column("risk_key", sa.String(64), nullable=False),
        sa.Column("service_name", sa.String(63), nullable=False),
        sa.Column("check_id", sa.String(64), nullable=False),
        sa.Column("resource", sa.String(256), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column(
            "opening_evidence_id", sa.Uuid(), sa.ForeignKey("evidence_ledger.id"), nullable=False
        ),
        sa.Column(
            "latest_evidence_id", sa.Uuid(), sa.ForeignKey("evidence_ledger.id"), nullable=False
        ),
        sa.Column("notification_evidence_id", sa.Uuid(), sa.ForeignKey("evidence_ledger.id")),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("risk_key"),
        sa.CheckConstraint("risk_key ~ '^[0-9a-f]{64}$'", name="key_valid"),
        sa.CheckConstraint(
            "category IN ('stability','capacity','security','cost')", name="category_valid"
        ),
        sa.CheckConstraint("outcome IN ('abnormal','unknown')", name="outcome_valid"),
        sa.CheckConstraint("episode >= 1 AND last_seen >= first_seen", name="observation_valid"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM inspection_risks")):
        raise ValueError("已有巡检风险，不能有损删除；请保留当前版本")
    op.drop_table("inspection_risks")
