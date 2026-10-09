"""人工回答草稿与独立问答审计类型。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_human_interaction"
down_revision: str | Sequence[str] | None = "0010_runbook_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_audit_log_audit_event_type"), "audit_log", type_="check")
    op.alter_column("audit_log", "event_type", existing_type=sa.String(16), type_=sa.String(17))
    op.create_check_constraint(
        "audit_event_type",
        "audit_log",
        "event_type IN ('tool_call','state_transition','approval','execution','human_interaction')",
    )
    op.create_table(
        "human_knowledge_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("wait_version", sa.Integer(), nullable=False),
        sa.Column("wait_status", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("respondent", sa.String(200), nullable=False),
        sa.Column("answer_evidence_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["ai_tasks.id"]),
        sa.ForeignKeyConstraint(
            ["answer_evidence_id", "task_id"], ["evidence_ledger.id", "evidence_ledger.task_id"]
        ),
        sa.UniqueConstraint("task_id", "wait_version"),
        sa.CheckConstraint("wait_version > 0", name="wait_version_positive"),
        sa.CheckConstraint(
            "wait_status IN ('NEED_HUMAN_JUDGMENT','WAITING_INFORMATION')", name="wait_status"
        ),
        sa.CheckConstraint("status = 'draft'", name="draft_status"),
        sa.CheckConstraint("length(trim(question)) BETWEEN 1 AND 3000", name="question_length"),
        sa.CheckConstraint("length(trim(answer)) BETWEEN 1 AND 8000", name="answer_length"),
        sa.CheckConstraint("length(trim(respondent)) BETWEEN 1 AND 200", name="respondent_length"),
    )


def downgrade() -> None:
    # 审计只追加：已有问答审计时拒绝降级，不删除历史或改写为审批。
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM audit_log WHERE event_type='human_interaction'")
    ):
        raise ValueError("存在人工问答审计，不能无损降级；请保留当前版本")
    op.drop_table("human_knowledge_drafts")
    op.drop_constraint(op.f("ck_audit_log_audit_event_type"), "audit_log", type_="check")
    op.alter_column("audit_log", "event_type", existing_type=sa.String(17), type_=sa.String(16))
    op.create_check_constraint(
        "audit_event_type",
        "audit_log",
        "event_type IN ('tool_call','state_transition','approval','execution')",
    )
