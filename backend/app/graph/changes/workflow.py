"""单次采集由 Temporal 执行和重试；时间窗在 Workflow 确定后保持不变。"""

from datetime import UTC, datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.connectors.changes.models import DeploymentQuery
    from app.graph.changes.schemas import TimelineInput, TimelineRequest, TimelineResult


@workflow.defn(name="ChangeTimelineWorkflow")
class ChangeTimelineWorkflow:
    @workflow.run
    async def run(self, value: TimelineInput) -> TimelineResult:
        try:
            if any(
                type(n) is not int or not 1 <= n <= maximum
                for n, maximum in (
                    (value.lookback_seconds, 2592000),
                    (value.activity_timeout_seconds, 3600),
                    (value.activity_max_attempts, 10),
                )
            ):
                raise ValueError
            end = datetime.fromisoformat(value.end) if value.end is not None else workflow.now()
            query = DeploymentQuery(
                service_name=value.service_name,
                start=end - timedelta(seconds=value.lookback_seconds),
                end=end,
            )
        except (TypeError, ValueError):
            raise ApplicationError(
                "Change Timeline Workflow 输入无效", non_retryable=True
            ) from None
        result: TimelineResult = await workflow.execute_activity(
            "timeline.collect",
            TimelineRequest(
                query.service_name,
                query.start.astimezone(UTC).isoformat(),
                query.end.astimezone(UTC).isoformat(),
            ),
            result_type=TimelineResult,
            start_to_close_timeout=timedelta(seconds=value.activity_timeout_seconds),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=5),
                maximum_attempts=value.activity_max_attempts,
            ),
        )
        return result
