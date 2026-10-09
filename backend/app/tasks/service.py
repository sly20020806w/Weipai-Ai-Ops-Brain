"""唯一任务状态迁移入口；事务提交由调用方负责，生命周期调度由 Temporal 负责。"""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utc_now
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.models import AITask, TaskStatusHistory, _status_write_scope
from app.tasks.states import TaskSource, TaskStatus, TransitionActor, validate_transition


class TaskNotFound(LookupError):
    pass


class TaskStateConflict(ValueError):
    pass


def _required_text(value: str, field: str, *, max_length: int | None = None) -> str:
    value = value.strip()
    if not value or (max_length is not None and len(value) > max_length):
        raise ValueError(f"{field} 必须非空且符合长度限制")
    return value


class TaskService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _require_transaction(self) -> None:
        if not self.session.in_transaction():
            raise RuntimeError("调用 tasks 服务前请使用 async with session.begin() 开启事务")

    async def create(self, *, source: TaskSource, title: str, reason: str) -> AITask:
        self._require_transaction()
        source = TaskSource(source)
        title = _required_text(title, "title", max_length=500)
        reason = _required_text(reason, "reason")
        async with self.session.begin_nested(), _status_write_scope():
            task = AITask(
                id=uuid4(), title=title, source=source, _status=TaskStatus.NEW, _status_version=0
            )
            self.session.add(task)
            await self.session.flush()
            changed_at = utc_now()
            self.session.add(
                TaskStatusHistory(
                    task_id=task.id,
                    sequence=0,
                    from_status=None,
                    to_status=TaskStatus.NEW,
                    reason=reason,
                    actor=TransitionActor.WORKFLOW,
                    changed_at=changed_at,
                )
            )
            await LedgerService(self.session).append_audit(
                task_id=task.id,
                event_type=AuditEventType.STATE_TRANSITION,
                actor=TransitionActor.WORKFLOW.value,
                operation="task.create",
                outcome="succeeded",
                details={
                    "from_status": None,
                    "to_status": TaskStatus.NEW.value,
                    "status_version": 0,
                    "reason": reason,
                },
                occurred_at=changed_at,
            )
        return task

    async def transition(
        self,
        task_id: UUID,
        target: TaskStatus,
        *,
        expected_status: TaskStatus,
        expected_version: int,
        reason: str,
        actor: TransitionActor = TransitionActor.WORKFLOW,
    ) -> AITask:
        self._require_transaction()
        target = TaskStatus(target)
        expected_status = TaskStatus(expected_status)
        actor = TransitionActor(actor)
        reason = _required_text(reason, "reason")
        async with self.session.begin_nested(), _status_write_scope():
            task = await self.session.scalar(
                select(AITask)
                .where(AITask.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task is None:
                raise TaskNotFound(f"任务不存在：{task_id}")
            if task.status != expected_status or task.status_version != expected_version:
                raise TaskStateConflict("任务状态或版本已变化，请重新读取后再迁移")
            from app.tasks.takeover import takeover_record

            if target is not TaskStatus.ESCALATED and await takeover_record(self.session, task.id):
                raise TaskStateConflict("人工已接管，任务自动化不可恢复")
            validate_transition(task.status, target, actor=actor)
            if target is TaskStatus.EXECUTING:
                from app.tasks.safety.service import require_automation_active

                await require_automation_active(self.session, task.id)
            if target is TaskStatus.RESOLVED:
                from app.verifier.authority import require_verification

                await require_verification(self.session, task)
            if target is TaskStatus.PLANNING:
                from app.tasks.review_gate import require_review_for_planning

                await require_review_for_planning(self.session, task)
            if target is TaskStatus.EXECUTING and task.status in {
                TaskStatus.PLANNING,
                TaskStatus.WAITING_APPROVAL,
            }:
                from app.tasks.approval.service import require_approval_for_execution

                await require_approval_for_execution(self.session, task)
            previous = task.status
            task._status = target
            task._status_version += 1
            task.updated_at = utc_now()
            self.session.add(
                TaskStatusHistory(
                    task_id=task.id,
                    sequence=task.status_version,
                    from_status=previous,
                    to_status=target,
                    reason=reason,
                    actor=actor,
                    changed_at=task.updated_at,
                )
            )
            await LedgerService(self.session).append_audit(
                task_id=task.id,
                event_type=AuditEventType.STATE_TRANSITION,
                actor=actor.value,
                operation="task.transition",
                outcome="succeeded",
                details={
                    "from_status": previous.value,
                    "to_status": target.value,
                    "status_version": task.status_version,
                    "reason": reason,
                },
                occurred_at=task.updated_at,
            )
        return task

    async def history(self, task_id: UUID) -> list[TaskStatusHistory]:
        records = await self.session.scalars(
            select(TaskStatusHistory)
            .where(TaskStatusHistory.task_id == task_id)
            .order_by(TaskStatusHistory.sequence)
        )
        return list(records)
