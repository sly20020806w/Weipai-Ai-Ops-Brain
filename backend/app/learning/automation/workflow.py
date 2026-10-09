"""确定性单轮扫描及派发；周期由 Temporal Schedule 驱动。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.learning.automation.models import AutomationInput, AutomationResult, ScanRequest
    from app.triggers.workflow import retry_policy


@workflow.defn(name="AutomationDiscoveryWorkflow")
class AutomationDiscoveryWorkflow:
    @workflow.run
    async def run(self, value: AutomationInput) -> AutomationResult:
        if (
            type(value.activity_timeout_seconds) is not int
            or not 1 <= value.activity_timeout_seconds <= 600
            or type(value.activity_max_attempts) is not int
            or not 1 <= value.activity_max_attempts <= 10
        ):
            raise ApplicationError("自动化发现 Workflow 配置无效", non_retryable=True)
        result: AutomationResult = await workflow.execute_activity(
            "automation.scan",
            ScanRequest(workflow.now().isoformat()),
            result_type=AutomationResult,
            start_to_close_timeout=timedelta(seconds=value.activity_timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=value.activity_max_attempts),
        )
        # 重复建议也重试派发，修复上一次提交后未派发的任务；start_task 固定 ID 去重。
        for receipt in result.receipts:
            await workflow.execute_activity(
                "event.start_task",
                receipt,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy(),
            )
        return result
