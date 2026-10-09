"""接入重试与 Watch 生命周期全部由 Temporal 托管。"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.triggers.schemas import EventBatch, EventReceipt, WatchInput, WatchResult


def retry_policy() -> RetryPolicy:
    # 暂时不可用持续由 Temporal 重试，不留下已落库但无人恢复的派发空隙。
    return RetryPolicy(
        initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=30)
    )


@workflow.defn(name="EventIngestionWorkflow")
class EventIngestionWorkflow:
    @workflow.run
    async def run(self, value: EventBatch) -> list[EventReceipt]:
        receipts: list[EventReceipt] = await workflow.execute_activity(
            "event.persist",
            value,
            result_type=list[EventReceipt],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy(),
        )
        for receipt in receipts:
            await workflow.execute_activity(
                "event.start_task",
                receipt,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy(),
            )
        return receipts


@workflow.defn(name="KubernetesEventWatchWorkflow")
class KubernetesEventWatchWorkflow:
    @workflow.run
    async def run(self, value: WatchInput) -> None:
        result = await workflow.execute_activity(
            "event.watch_kubernetes",
            value,
            result_type=WatchResult,
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=retry_policy(),
        )
        if result.events:
            await workflow.execute_child_workflow(
                EventIngestionWorkflow.run,
                EventBatch(result.events),
                id=f"{workflow.info().workflow_id}/batch/{workflow.info().run_id}",
            )
        # 只有入库与派发成功后才推进 cursor；历史有界，进程重启不丢进度。
        workflow.continue_as_new(WatchInput(value.namespace, result.resource_version))
