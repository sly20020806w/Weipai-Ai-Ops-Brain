"""人工操作的持久化命令：先提交授权/回答/停止记录，再可靠派发信号。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class TaskControlWorkflow:
    @workflow.run
    async def run(self, command_json: str) -> str:
        # Activity 重试复用数据库行锁与既有审批/问答的幂等结果。
        receipt: str = await workflow.execute_activity(
            "task.control.record",
            command_json,
            result_type=str,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        await workflow.execute_activity(
            "task.control.deliver",
            args=[command_json, receipt],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        return receipt
