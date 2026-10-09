"""API 只启动/等待 Temporal；SSE 断开不取消已接纳的任务。"""

import asyncio
from uuid import UUID, uuid4

from temporalio.client import Client, WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.service import RPCError

from app.agent.chat.models import ChatAnswer, ChatSubmission
from app.agent.chat.service import read_answer
from app.agent.chat.workflow import ChatIngestionWorkflow
from app.config import Settings
from app.db.session import Database
from app.tasks.states import TaskStatus
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import WorkflowProgress
from app.triggers.schemas import EventReceipt


def ready(progress: WorkflowProgress) -> bool:
    return progress.task is not None and (
        progress.task.status
        in {TaskStatus.CLOSED, TaskStatus.ESCALATED, TaskStatus.AUTOMATION_ABORTED}
        or progress.approval_prompt is not None
        or (
            progress.action_plan_json is not None
            and progress.task.status is TaskStatus.WAITING_INFORMATION
        )
    )


class ChatGateway:
    def __init__(self, client: Client, database: Database, settings: Settings) -> None:
        self.client, self.database, self.settings = client, database, settings

    async def submit(self, value: ChatSubmission) -> EventReceipt:
        handle = await self.client.start_workflow(
            ChatIngestionWorkflow.run,
            value.model_dump_json(),
            id=f"chat-ingest-{uuid4()}",
            task_queue=self.settings.temporal_config.task_queue,
        )
        return await asyncio.wait_for(handle.result(), 30)

    async def answer(self, task_id: UUID) -> ChatAnswer:
        async with self.database.session() as session:
            return await read_answer(session, task_id)

    async def wait(self, receipt: EventReceipt) -> ChatAnswer:
        handle = self.client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
        progress = await handle.query(AITaskWorkflow.progress)
        if not ready(progress):
            try:
                await handle.execute_update(AITaskWorkflow.chat_response)
            except (WorkflowUpdateFailedError, RPCError):
                # Workflow 已在 query/update 间结束；读取持久化终态，不重新发起生命周期。
                if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                    raise
                await handle.result()
        return await self.answer(UUID(receipt.task_id))
