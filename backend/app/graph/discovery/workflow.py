"""每次 Workflow 只做一次发现；周期与不重叠策略交给 Temporal Schedule。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.graph.discovery.models import DiscoveryInput, DiscoveryRequest, DiscoveryResult


@workflow.defn(name="DiscoveryWorkflow")
class DiscoveryWorkflow:
    @workflow.run
    async def run(self, value: DiscoveryInput) -> DiscoveryResult:
        if any(
            type(number) is not int or not 1 <= number <= maximum
            for number, maximum in (
                (value.lookback_seconds, 86400),
                (value.activity_timeout_seconds, 3600),
                (value.activity_max_attempts, 10),
            )
        ):
            raise ApplicationError("Discovery Workflow 输入无效", non_retryable=True)
        result: DiscoveryResult = await workflow.execute_activity(
            "discovery.refresh",
            DiscoveryRequest(workflow.now().isoformat(), value.lookback_seconds),
            result_type=DiscoveryResult,
            start_to_close_timeout=timedelta(seconds=value.activity_timeout_seconds),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=5),
                maximum_attempts=value.activity_max_attempts,
            ),
        )
        return result
