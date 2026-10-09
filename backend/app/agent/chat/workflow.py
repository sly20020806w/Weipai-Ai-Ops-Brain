"""对话输入落库与统一任务派发均由 Temporal 重试。"""

from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.triggers.schemas import EventReceipt


@workflow.defn
class ChatIngestionWorkflow:
    @workflow.run
    async def run(self, submission_json: str) -> EventReceipt:
        receipt = await workflow.execute_activity(
            "chat.persist",
            submission_json,
            result_type=EventReceipt,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        await workflow.execute_activity(
            "event.start_task",
            receipt,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return cast(EventReceipt, receipt)
