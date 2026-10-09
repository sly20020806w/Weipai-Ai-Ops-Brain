"""只追加的证据与审计；数据库触发器同时保护原始 SQL 写入路径。"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.schema import Table

from app.db.base import Base, UTCDateTime, utc_now


class AuditEventType(StrEnum):
    TOOL_CALL = "tool_call"
    STATE_TRANSITION = "state_transition"
    APPROVAL = "approval"
    EXECUTION = "execution"
    HUMAN_INTERACTION = "human_interaction"


class AppendOnlyViolation(ValueError):
    pass


class Evidence(Base):
    __tablename__ = "evidence_ledger"
    __table_args__ = (
        CheckConstraint("length(trim(source_tool)) > 0", name="source_tool_not_blank"),
        CheckConstraint("jsonb_typeof(parameters) = 'object'", name="parameters_object"),
        CheckConstraint(
            "result_snapshot IS NOT NULL OR source_reference IS NOT NULL", name="result_required"
        ),
        CheckConstraint(
            "source_reference IS NULL OR length(trim(source_reference)) > 0",
            name="source_reference_not_blank",
        ),
        UniqueConstraint("id", "task_id"),
        Index("ix_evidence_ledger_task_time", "task_id", "collected_at", "id"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("ai_tasks.id"), nullable=False)
    source_tool: Mapped[str] = mapped_column(String(200), nullable=False)
    parameters: Mapped[dict[str, JsonValue]] = mapped_column(JSONB(), nullable=False)
    result_snapshot: Mapped[JsonValue | None] = mapped_column(JSONB(none_as_null=True))
    source_reference: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class AuditRecord(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("length(trim(actor)) > 0", name="actor_not_blank"),
        CheckConstraint("length(trim(operation)) > 0", name="operation_not_blank"),
        CheckConstraint("length(trim(outcome)) > 0", name="outcome_not_blank"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
        ForeignKeyConstraint(
            ["evidence_id", "task_id"], ["evidence_ledger.id", "evidence_ledger.task_id"]
        ),
        Index("ix_audit_log_task_time", "task_id", "occurred_at", "id"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("ai_tasks.id"), nullable=False)
    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(
            AuditEventType,
            name="audit_event_type",
            values_callable=lambda values: [value.value for value in values],
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    operation: Mapped[str] = mapped_column(String(200), nullable=False)
    outcome: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict[str, JsonValue]] = mapped_column(JSONB(), nullable=False)
    evidence_id: Mapped[UUID | None] = mapped_column(nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class CatalogAudit(Base):
    """本人编辑平台知识/Runbook 的审计，不伪造运维任务或放宽任务审计外键。"""

    __tablename__ = "catalog_audit_log"
    __table_args__ = (
        CheckConstraint("length(trim(actor)) > 0", name="actor_not_blank"),
        CheckConstraint(
            "operation IN ('knowledge.create','knowledge.update','knowledge.delete',"
            "'runbooks.create','runbooks.update','runbooks.delete')",
            name="operation_valid",
        ),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
        Index("ix_catalog_audit_log_time", "occurred_at", "id"),
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    operation: Mapped[str] = mapped_column(String(200), nullable=False)
    details: Mapped[dict[str, JsonValue]] = mapped_column(JSONB(), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    @property
    def task_id(self) -> None:
        return None

    @property
    def evidence_id(self) -> None:
        return None

    @property
    def event_type(self) -> str:
        return "catalog_edit"

    @property
    def outcome(self) -> str:
        return "recorded"


@event.listens_for(Session, "before_flush")
def _guard_append_only_flush(session: Session, flush_context: object, instances: object) -> None:
    for record in session.deleted:
        if isinstance(record, (Evidence, AuditRecord, CatalogAudit)):
            raise AppendOnlyViolation("证据与审计只允许追加，不允许删除")
    for record in session.dirty:
        if isinstance(record, (Evidence, AuditRecord, CatalogAudit)) and session.is_modified(
            record
        ):
            raise AppendOnlyViolation("证据与审计只允许追加，不允许更新")


@event.listens_for(Session, "do_orm_execute")
def _guard_append_only_bulk_writes(state: ORMExecuteState) -> None:
    statement = state.statement
    if (
        isinstance(statement, (Update, Delete))
        and isinstance(statement.table, Table)
        and statement.table.name in {"evidence_ledger", "audit_log", "catalog_audit_log"}
    ):
        raise AppendOnlyViolation("证据与审计只允许追加，不允许批量更新或删除")
