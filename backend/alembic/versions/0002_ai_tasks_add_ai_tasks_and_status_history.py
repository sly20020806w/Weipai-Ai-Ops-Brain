"""建立 AI Task 及状态历史表。

Revision ID: 0002_ai_tasks
Revises: 0001_database_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_ai_tasks"
down_revision: str | Sequence[str] | None = "0001_database_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_tasks",
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "Alert",
                "Ticket",
                "Schedule",
                "State",
                "Prediction",
                "Release",
                "Human",
                "AI",
                name="task_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "NEW",
                "CONTEXT_BUILDING",
                "RUNBOOK_MATCHING",
                "INVESTIGATING",
                "RCA",
                "PLANNING",
                "NEED_HUMAN_JUDGMENT",
                "WAITING_INFORMATION",
                "WAITING_APPROVAL",
                "EXECUTING",
                "VERIFYING",
                "RESOLVED",
                "FAILED",
                "AUTOMATION_ABORTED",
                "ESCALATED",
                "LEARNING",
                "CLOSED",
                name="task_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("status_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name=op.f("ck_ai_tasks_title_not_blank")),
        sa.CheckConstraint(
            "status_version >= 0", name=op.f("ck_ai_tasks_status_version_nonnegative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_tasks")),
    )
    op.create_index("ix_ai_tasks_source", "ai_tasks", ["source"], unique=False)
    op.create_index("ix_ai_tasks_status", "ai_tasks", ["status"], unique=False)
    op.create_table(
        "ai_task_status_history",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "from_status",
            sa.Enum(
                "NEW",
                "CONTEXT_BUILDING",
                "RUNBOOK_MATCHING",
                "INVESTIGATING",
                "RCA",
                "PLANNING",
                "NEED_HUMAN_JUDGMENT",
                "WAITING_INFORMATION",
                "WAITING_APPROVAL",
                "EXECUTING",
                "VERIFYING",
                "RESOLVED",
                "FAILED",
                "AUTOMATION_ABORTED",
                "ESCALATED",
                "LEARNING",
                "CLOSED",
                name="from_task_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(
                "NEW",
                "CONTEXT_BUILDING",
                "RUNBOOK_MATCHING",
                "INVESTIGATING",
                "RCA",
                "PLANNING",
                "NEED_HUMAN_JUDGMENT",
                "WAITING_INFORMATION",
                "WAITING_APPROVAL",
                "EXECUTING",
                "VERIFYING",
                "RESOLVED",
                "FAILED",
                "AUTOMATION_ABORTED",
                "ESCALATED",
                "LEARNING",
                "CLOSED",
                name="to_task_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "actor",
            sa.Enum(
                "workflow",
                "verifier",
                name="transition_actor",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(sequence = 0 AND from_status IS NULL AND to_status = 'NEW') OR "
            "(sequence > 0 AND from_status IS NOT NULL AND from_status <> to_status)",
            name=op.f("ck_ai_task_status_history_transition_shape"),
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_ai_task_status_history_reason_not_blank")
        ),
        sa.CheckConstraint(
            "sequence >= 0", name=op.f("ck_ai_task_status_history_sequence_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["ai_tasks.id"], name=op.f("fk_ai_task_status_history_task_id_ai_tasks")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_task_status_history")),
        sa.UniqueConstraint("task_id", "sequence", name=op.f("uq_ai_task_status_history_task_id")),
    )
    op.create_index(
        "ix_ai_task_status_history_task_time",
        "ai_task_status_history",
        ["task_id", "changed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_task_status_history_task_time", table_name="ai_task_status_history")
    op.drop_table("ai_task_status_history")
    op.drop_index("ix_ai_tasks_status", table_name="ai_tasks")
    op.drop_index("ix_ai_tasks_source", table_name="ai_tasks")
    op.drop_table("ai_tasks")
