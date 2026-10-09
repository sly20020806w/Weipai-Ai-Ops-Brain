"""Workflow 保存本轮采集时间、重试和任务派发；不创建自己的周期循环。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.triggers.detection.models import (
        DetectionBatch,
        DetectionInput,
        DetectionRequest,
        DetectionResult,
    )
    from app.triggers.workflow import retry_policy


@workflow.defn(name="StatePredictionWorkflow")
class StatePredictionWorkflow:
    @workflow.run
    async def run(self, value: DetectionInput) -> DetectionResult:
        if (
            type(value.activity_timeout_seconds) is not int
            or not 1 <= value.activity_timeout_seconds <= 600
            or type(value.activity_max_attempts) is not int
            or not 1 <= value.activity_max_attempts <= 10
        ):
            raise ApplicationError("检测 Workflow 配置无效", non_retryable=True)
        batch: DetectionBatch = await workflow.execute_activity(
            "detection.collect",
            DetectionRequest(workflow.now().isoformat()),
            result_type=DetectionBatch,
            start_to_close_timeout=timedelta(seconds=value.activity_timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=value.activity_max_attempts),
        )
        result: DetectionResult = await workflow.execute_activity(
            "detection.persist",
            batch,
            result_type=DetectionResult,
            start_to_close_timeout=timedelta(seconds=value.activity_timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=value.activity_max_attempts),
        )
        for receipt in result.receipts:
            await workflow.execute_activity(
                "event.start_task",
                receipt,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy(),
            )
        return result
