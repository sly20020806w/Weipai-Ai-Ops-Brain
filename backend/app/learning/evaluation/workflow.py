"""离线历史重调查由 Temporal 承担执行、重试和恢复。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class ReplayEvaluationWorkflow:
    @workflow.run
    async def run(self, request_json: str) -> str:
        result: str = await workflow.execute_activity(
            "learning.replay",
            request_json,
            result_type=str,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return result
