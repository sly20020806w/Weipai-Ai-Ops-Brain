"""任务与状态历史模型；状态写入仅由 tasks 服务开放。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.schema import Table

from app.db.base import Base, UTCDateTime, utc_now
from app.tasks.states import TaskSource, TaskStatus, TransitionActor

_status_write_allowed: ContextVar[bool] = ContextVar("task_status_write_allowed", default=False)


class TaskServiceRequired(ValueError):
    pass


@asynccontextmanager
async def _status_write_scope() -> AsyncIterator[None]:
    token = _status_write_allowed.set(True)
    try:
        yield
    finally:
        _status_write_allowed.reset(token)


def _enum_values(
    enum_type: type[TaskSource] | type[TaskStatus] | type[TransitionActor],
) -> list[str]:
    return [item.value for item in enum_type]


class AITask(Base):
    __tablename__ = "ai_tasks"
    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        CheckConstraint("status_version >= 0", name="status_version_nonnegative"),
        Index("ix_ai_tasks_status", "status"),
        Index("ix_ai_tasks_source", "source"),
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[TaskSource] = mapped_column(
        Enum(
            TaskSource,
            name="task_source",
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    _status: Mapped[TaskStatus] = mapped_column(
        "status",
        Enum(
            TaskStatus,
            name="task_status",
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    _status_version: Mapped[int] = mapped_column("status_version", nullable=False)

    @property
    def status(self) -> TaskStatus:
        return self._status

    @property
    def status_version(self) -> int:
        return self._status_version


class TaskStatusHistory(Base):
    __tablename__ = "ai_task_status_history"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_not_blank"),
        CheckConstraint(
            "(sequence = 0 AND from_status IS NULL AND to_status = 'NEW') OR "
            "(sequence > 0 AND from_status IS NOT NULL AND from_status <> to_status)",
            name="transition_shape",
        ),
        Index("ix_ai_task_status_history_task_time", "task_id", "changed_at"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("ai_tasks.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    from_status: Mapped[TaskStatus | None] = mapped_column(
        Enum(
            TaskStatus,
            name="from_task_status",
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=True,
    )
    to_status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="to_task_status",
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[TransitionActor] = mapped_column(
        Enum(
            TransitionActor,
            name="transition_actor",
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


@event.listens_for(AITask._status, "set")
@event.listens_for(AITask._status_version, "set")
def _guard_status_assignment(
    target: AITask, value: object, oldvalue: object, initiator: object
) -> None:
    if not _status_write_allowed.get():
        raise TaskServiceRequired("任务状态只能经 tasks 服务迁移")


@event.listens_for(Session, "before_flush")
def _guard_task_flush(session: Session, flush_context: object, instances: object) -> None:
    if _status_write_allowed.get():
        return
    for record in session.new:
        if isinstance(record, (AITask, TaskStatusHistory)):
            raise TaskServiceRequired("任务及状态历史只能经 tasks 服务创建")
    for record in session.dirty:
        if isinstance(record, AITask):
            state = sa_inspect(record)
            if (
                state.attrs._status.history.has_changes()
                or state.attrs._status_version.history.has_changes()
            ):
                raise TaskServiceRequired("任务状态只能经 tasks 服务迁移")


@event.listens_for(Session, "do_orm_execute")
def _guard_bulk_task_writes(state: ORMExecuteState) -> None:
    statement = state.statement
    if (
        isinstance(statement, (Insert, Update, Delete))
        and isinstance(statement.table, Table)
        and statement.table.name
        in {
            "ai_tasks",
            "ai_task_status_history",
        }
    ):
        raise TaskServiceRequired("任务及状态历史不允许批量写入，请使用 tasks 服务")
