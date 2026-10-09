"""Activity 使用独立事务，状态/历史/审计仍只经 TaskService 写入。"""

from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.db.session import Database
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.service import TaskNotFound, TaskService, TaskStateConflict
from app.tasks.states import InvalidTaskTransition, TaskStatus, TransitionActor
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest


class TaskActivityStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def initial(self, task_id: str) -> TaskSnapshot:
        async with self.database.session() as session:
            task = await session.get(AITask, UUID(task_id))
            if task is None:
                raise TaskNotFound("Workflow 任务不存在")
            if task.status is not TaskStatus.NEW or task.status_version != 0:
                raise TaskStateConflict("新 Workflow 只能接管 NEW、version=0 的任务")
            return TaskSnapshot(task_id, task.status, task.status_version)

    async def transition(
        self, request: TransitionRequest, *, actor: TransitionActor = TransitionActor.WORKFLOW
    ) -> TaskSnapshot:
        task_id = UUID(request.task.task_id)
        sequence = request.task.version + 1
        async with self.database.session() as session, session.begin():
            # 同一事务先锁定任务，再检查指定序号，涵盖提交后丢失响应及并发重试。
            task = await session.scalar(
                select(AITask).where(AITask.id == task_id).with_for_update()
            )
            if task is None:
                raise TaskNotFound("Workflow 任务不存在")
            previous = await session.scalar(
                select(TaskStatusHistory).where(
                    TaskStatusHistory.task_id == task_id,
                    TaskStatusHistory.sequence == sequence,
                )
            )
            if previous is not None:
                if (
                    previous.from_status != request.task.status
                    or previous.to_status != request.target
                    or previous.reason != request.reason
                    or previous.actor != actor
                ):
                    raise TaskStateConflict("同一迁移序号已被其他请求使用")
                return TaskSnapshot(request.task.task_id, previous.to_status, sequence)
            task = await TaskService(session).transition(
                task_id,
                request.target,
                expected_status=request.task.status,
                expected_version=request.task.version,
                reason=request.reason,
                actor=actor,
            )
            return TaskSnapshot(request.task.task_id, task.status, task.status_version)


class TaskActivities:
    def __init__(self, store: TaskActivityStore) -> None:
        self.store = store

    @activity.defn(name="task.load")
    async def load(self, task_id: str) -> TaskSnapshot:
        try:
            return await self.store.initial(task_id)
        except (TaskNotFound, TaskStateConflict, ValueError) as error:
            raise ApplicationError(
                "任务初始化被拒绝", type=type(error).__name__, non_retryable=True
            ) from None

    @activity.defn(name="task.transition")
    async def transition(self, request: TransitionRequest) -> TaskSnapshot:
        try:
            return await self.store.transition(request)
        except (TaskNotFound, TaskStateConflict, InvalidTaskTransition, ValueError) as error:
            raise ApplicationError(
                "任务状态迁移被拒绝", type=type(error).__name__, non_retryable=True
            ) from None

    @activity.defn(name="task.placeholder_stage")
    async def placeholder_stage(self, task: TaskSnapshot) -> None:
        # 本步骤不接入 Agent、Runbook、Executor 或学习业务；没有运维副作用。
        if task.status not in {
            TaskStatus.CONTEXT_BUILDING,
            TaskStatus.RUNBOOK_MATCHING,
            TaskStatus.INVESTIGATING,
            TaskStatus.RCA,
            TaskStatus.PLANNING,
            TaskStatus.EXECUTING,
            TaskStatus.LEARNING,
        }:
            raise ApplicationError("非占位阶段", non_retryable=True)
