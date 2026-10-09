"""认知目录编辑审计：独立只追加表，原任务/Evidence 外键保持强约束。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_catalog_audit"
down_revision: str | Sequence[str] | None = "0015_single_user_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("operation", sa.String(200), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(trim(actor)) > 0", name="actor_not_blank"),
        sa.CheckConstraint(
            "operation IN ('knowledge.create','knowledge.update','knowledge.delete',"
            "'runbooks.create','runbooks.update','runbooks.delete')",
            name="operation_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
    )
    op.create_index("ix_catalog_audit_log_time", "catalog_audit_log", ["occurred_at", "id"])
    op.execute(
        "CREATE TRIGGER catalog_audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON catalog_audit_log FOR EACH STATEMENT EXECUTE FUNCTION reject_ledger_mutation()"
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM catalog_audit_log)")):
        raise RuntimeError("目录编辑审计已存在，拒绝有损降级")
    op.drop_table("catalog_audit_log")
