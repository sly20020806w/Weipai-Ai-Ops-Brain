"""Temporal 保存日历触发与发布验证 Timer；业务代码不轮询调度。"""

from datetime import timedelta
from hashlib import sha256
from uuid import UUID

from temporalio import workflow
from temporalio.common import SearchAttributeKey
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.tasks.states import TaskSource
    from app.triggers.scheduling.models import (
        PERIODIC_TITLES,
        PeriodicInput,
        ReleaseVerificationInput,
    )
    from app.triggers.schemas import EventBatch, EventReceipt, NormalizedEvent
    from app.triggers.workflow import EventIngestionWorkflow


async def ingest(event: NormalizedEvent) -> EventReceipt:
    receipts = await workflow.execute_child_workflow(
        EventIngestionWorkflow.run,
        EventBatch([event.model_dump_json()]),
        id=f"{workflow.info().workflow_id}/ingest",
    )
    return receipts[0]


@workflow.defn(name="PeriodicTriggerWorkflow")
class PeriodicTriggerWorkflow:
    @workflow.run
    async def run(self, value: PeriodicInput) -> EventReceipt:
        if value.kind not in PERIODIC_TITLES:
            raise ApplicationError("未知周期触发类型", non_retryable=True)
        scheduled_at = workflow.info().typed_search_attributes.get(
            SearchAttributeKey.for_datetime("TemporalScheduledStartTime")
        )
        event = NormalizedEvent(
            origin="schedule",
            source=TaskSource.SCHEDULE,
            external_id=f"{value.kind}:{sha256(workflow.info().workflow_id.encode()).hexdigest()}",
            service_name=value.service_name,
            title=PERIODIC_TITLES[value.kind],
            occurred_at=scheduled_at or workflow.now(),
        )
        return await ingest(event)


@workflow.defn(name="ReleaseVerificationTriggerWorkflow")
class ReleaseVerificationTriggerWorkflow:
    @workflow.run
    async def run(self, value: ReleaseVerificationInput) -> EventReceipt:
        try:
            UUID(value.event_id)
            if not 1 <= value.delay_seconds <= 86400:
                raise ValueError("发布验证延迟无效")
            release = NormalizedEvent(
                origin="schedule",
                source=TaskSource.SCHEDULE,
                external_id=f"release-verification:{value.event_id}",
                service_name=value.service_name,
                title=f"{value.service_name} 发布后上线验证",
                occurred_at=value.occurred_at,
            )
        except ValueError:
            raise ApplicationError("发布验证触发参数无效", non_retryable=True) from None
        due_at = release.occurred_at + timedelta(seconds=value.delay_seconds)
        # 迟到的发布事件立即补触发；重启/历史回放不会重算或重置到期时间。
        await workflow.sleep(max(timedelta(), due_at - workflow.now()))
        return await ingest(release.model_copy(update={"occurred_at": due_at}))
