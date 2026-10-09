"""巡检子 Workflow 不拥有任务状态；父级统一 AI Task 负责生命周期。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.tasks.inspection.models import InspectionRequest, InspectionResult


@workflow.defn(name="InspectionWorkflow")
class InspectionWorkflow:
    @workflow.run
    async def run(self, request: InspectionRequest) -> InspectionResult:
        result: InspectionResult = await workflow.execute_activity(
            "inspection.scan",
            request,
            result_type=InspectionResult,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        for risk_id in result.risk_ids:
            await workflow.execute_activity(
                "inspection.notify",
                risk_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        return result
