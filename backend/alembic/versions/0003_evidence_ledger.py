"""建立只追加的 Evidence Ledger 与审计日志。

Revision ID: 0003_evidence_ledger
Revises: 0002_ai_tasks
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_evidence_ledger"
down_revision: str | Sequence[str] | None = "0002_ai_tasks"
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
        "evidence_ledger",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("source_tool", sa.String(200), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("result_snapshot", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        *_common_columns(),
        sa.CheckConstraint("length(trim(source_tool)) > 0", name="source_tool_not_blank"),
        sa.CheckConstraint("jsonb_typeof(parameters) = 'object'", name="parameters_object"),
        sa.CheckConstraint(
            "result_snapshot IS NOT NULL OR source_reference IS NOT NULL", name="result_required"
        ),
        sa.CheckConstraint(
            "source_reference IS NULL OR length(trim(source_reference)) > 0",
            name="source_reference_not_blank",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["ai_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "task_id"),
    )
    op.create_index(
        "ix_evidence_ledger_task_time", "evidence_ledger", ["task_id", "collected_at", "id"]
    )
    op.create_table(
        "audit_log",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "tool_call",
                "state_transition",
                "approval",
                "execution",
                name="audit_event_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("operation", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(100), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_common_columns(),
        sa.CheckConstraint("length(trim(actor)) > 0", name="actor_not_blank"),
        sa.CheckConstraint("length(trim(operation)) > 0", name="operation_not_blank"),
        sa.CheckConstraint("length(trim(outcome)) > 0", name="outcome_not_blank"),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
        sa.ForeignKeyConstraint(["task_id"], ["ai_tasks.id"]),
        sa.ForeignKeyConstraint(
            ["evidence_id", "task_id"], ["evidence_ledger.id", "evidence_ledger.task_id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_task_time", "audit_log", ["task_id", "occurred_at", "id"])
    op.execute(
        """
        CREATE FUNCTION reject_ledger_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only ledger: % on % is forbidden', TG_OP, TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table in ("evidence_ledger", "audit_log"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_ledger_mutation()"
        )


def downgrade() -> None:
    for table in ("audit_log", "evidence_ledger"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
        op.drop_table(table)
    op.execute("DROP FUNCTION reject_ledger_mutation()")
