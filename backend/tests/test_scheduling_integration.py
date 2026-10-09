"""Step 22：真实 Schedule 日历回填、官方时间跳跃与独立本地数据库验收。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from temporalio import activity
from temporalio.client import (
    Client,
    ScheduleActionExecutionStartWorkflow,
    ScheduleBackfill,
    WorkflowExecutionStatus,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from app.config import Settings, parse_database_url
from app.db.session import Database
from app.tasks.config import TemporalConfig
from app.tasks.models import AITask
from app.tasks.states import TaskSource
from app.tasks.worker import create_worker
from app.triggers.activities import EventActivities
from app.triggers.models import OpsEvent
from app.triggers.scheduling.config import SchedulingConfig
from app.triggers.scheduling.models import PeriodicInput, ReleaseVerificationInput
from app.triggers.scheduling.schedule import ensure_periodic_schedules
from app.triggers.scheduling.workflow import (
    PeriodicTriggerWorkflow,
    ReleaseVerificationTriggerWorkflow,
)
from app.triggers.schemas import EventBatch, EventReceipt, NormalizedEvent
from app.triggers.workflow import EventIngestionWorkflow
from tests.database_support import get_test_database_url, migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-schedules.ps1"
)
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本地 Temporal 专项入口"
)
time_skipping = pytest.mark.skipif(
    not os.environ.get("TEST_TIME_SKIPPING_SERVER"), reason="需预先缓存官方时间跳跃服务器"
)


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        yield instance
    finally:
        await instance.dispose()


def settings(queue: str) -> Settings:
    return Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG=TemporalConfig(task_queue=queue),
        SCHEDULING_CONFIG=SchedulingConfig(schedule_prefix=f"schedule-test-{uuid4().hex}"),
        # 本专项只验收 Step 22 的日历/触发，实际巡检闭环由 Step 40 专项覆盖。
        INSPECTION_CONFIG={"enabled": False},
    )


async def verify_receipt(database: Database, receipt: EventReceipt, occurred: datetime) -> None:
    async with database.session() as session:
        event = await session.get(OpsEvent, UUID(receipt.event_id))
        task = await session.get(AITask, UUID(receipt.task_id))
        assert event is not None and event.origin == "schedule" and event.source == "Schedule"
        assert event.occurred_at == occurred and event.occurred_at.tzinfo is UTC
        assert task is not None and task.source is TaskSource.SCHEDULE


@local_temporal
@pytest.mark.asyncio
async def test_three_schedules_calendar_backfill_create_one_task_each_and_preserve_pause(
    database: Database,
) -> None:
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"], namespace="default")
    config = settings(f"schedule-test-{uuid4().hex}")
    ids = await ensure_periodic_schedules(
        client, config.scheduling_config, config.temporal_config.task_queue
    )
    task_ids: list[str] = []
    # Temporal 原生日历回填跳至各自匹配时刻，不把间隔缩成秒或用自制日历。
    slots = [datetime(2026, 10, 5, hour, tzinfo=UTC) for hour in (1, 2, 10)]
    try:
        assert len(ids) == 3
        # Schedule 列表依赖异步可见性索引，不能把创建成功后的瞬时延迟判成丢失。
        async with asyncio.timeout(30):
            while not set(ids) <= {entry.id async for entry in await client.list_schedules()}:
                await asyncio.sleep(0.05)
        for name in ids:
            await client.get_schedule_handle(name).pause(note="测试只运行指定回填窗口")
        assert not await ensure_periodic_schedules(
            client, config.scheduling_config, config.temporal_config.task_queue
        )
        async with create_worker(client, database, config):
            for name, slot in zip(ids, slots, strict=True):
                handle = client.get_schedule_handle(name)
                assert (await handle.describe()).schedule.state.paused
                await handle.backfill(
                    ScheduleBackfill(slot - timedelta(seconds=1), slot + timedelta(seconds=1))
                )
                async with asyncio.timeout(30):
                    while not (description := await handle.describe()).info.recent_actions:
                        await asyncio.sleep(0.05)
                assert description.info.num_actions == 1
                action = description.info.recent_actions[0].action
                assert isinstance(action, ScheduleActionExecutionStartWorkflow)
                execution = client.get_workflow_handle(action.workflow_id, result_type=EventReceipt)
                receipt = await asyncio.wait_for(execution.result(), 30)
                task_ids.append(receipt.task_id)
                await verify_receipt(database, receipt, slot)
                assert (
                    await client.get_workflow_handle(receipt.workflow_id).describe()
                ).status is WorkflowExecutionStatus.RUNNING
                await Replayer(workflows=[PeriodicTriggerWorkflow]).replay_workflow(
                    await execution.fetch_history()
                )
        assert len(set(task_ids)) == 3
        # 工作日巡检在星期日同一时刻不匹配。
        sunday = datetime(2026, 10, 4, 1, tzinfo=UTC)
        await client.get_schedule_handle(ids[0]).backfill(
            ScheduleBackfill(sunday - timedelta(seconds=1), sunday + timedelta(seconds=1))
        )
        assert (await client.get_schedule_handle(ids[0]).describe()).info.num_actions == 1
    finally:
        for name in ids:
            await client.get_schedule_handle(name).delete()
        for task_id in task_ids:
            await client.get_workflow_handle(f"ai-task-{task_id}").terminate("验收清理")


@time_skipping
@pytest.mark.asyncio
async def test_release_ten_minutes_not_early_duplicate_retry_restart_and_replay(
    database: Database,
) -> None:
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ["TEST_TIME_SKIPPING_SERVER"]
    ) as env:
        config = settings(f"release-test-{uuid4().hex}")
        now = await env.get_current_time()
        release = NormalizedEvent(
            origin="argocd",
            source=TaskSource.RELEASE,
            external_id=uuid4().hex,
            service_name=f"payment-{uuid4().hex}",
            title="发布 v2.3.7",
            occurred_at=now,
        )
        with env.auto_time_skipping_disabled():
            # 官方测试服务器没有完整的 sticky queue 超时行为；禁用缓存强制历史重放。
            async with create_worker(env.client, database, config, max_cached_workflows=0):
                execution = await env.client.start_workflow(
                    EventIngestionWorkflow.run,
                    EventBatch([release.model_dump_json()]),
                    id=f"release-ingest-{uuid4().hex}",
                    task_queue=config.temporal_config.task_queue,
                )
                original = (await execution.result())[0]
                # 重投同一源事件，不创建第二个发布任务或重置验证 Timer。
                duplicate = await env.client.execute_workflow(
                    EventIngestionWorkflow.run,
                    EventBatch([release.model_dump_json()]),
                    id=f"release-ingest-{uuid4().hex}",
                    task_queue=config.temporal_config.task_queue,
                )
                assert duplicate[0].duplicate and duplicate[0].event_id == original.event_id
                timer = env.client.get_workflow_handle(
                    f"release-verification-{original.event_id}",
                    run_id=(
                        await env.client.get_workflow_handle(
                            f"release-verification-{original.event_id}"
                        ).describe()
                    ).run_id,
                    result_type=EventReceipt,
                )
                async with asyncio.timeout(20):
                    while not any(
                        e.HasField("timer_started_event_attributes")
                        for e in (await timer.fetch_history()).events
                    ):
                        await asyncio.sleep(0.01)
                history = await timer.fetch_history()
                durations = [
                    e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
                    for e in history.events
                    if e.HasField("timer_started_event_attributes")
                ]
                assert len(durations) == 1 and timedelta(seconds=590) < durations[0] <= timedelta(
                    seconds=600
                )
                # 先让发布任务完成其当前 Activity 并进入人工等待 Timer。
                # 测试服务器在有待处理 Workflow Task/Activity 时不会跳过时间。
                task_handle = env.client.get_workflow_handle(original.workflow_id)
                async with asyncio.timeout(20):
                    while not any(
                        event.HasField("timer_started_event_attributes")
                        for event in (await task_handle.fetch_history()).events
                    ):
                        await asyncio.sleep(0.01)
            # Worker 已停止，跳过前 599 秒；数据库中只有发布事件。
            due = now + timedelta(seconds=600)
            await env.sleep(due - await env.get_current_time() - timedelta(seconds=1))
            async with database.session() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OpsEvent)
                        .where(OpsEvent.service_name == release.service_name)
                    )
                    == 1
                )
            # 重启 Worker，跨过第 600 秒后恰好产生上线验证。
            async with create_worker(env.client, database, config, max_cached_workflows=0):
                await env.sleep(timedelta(seconds=2))
                receipt = await asyncio.wait_for(timer.result(), 30)
                await verify_receipt(database, receipt, due)
                async with database.session() as session:
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(OpsEvent)
                            .where(OpsEvent.service_name == release.service_name)
                        )
                        == 2
                    )
                events = EventActivities(database, config, env.client)
                await events.start_task(original)  # 派发提交后丢响应重试，已完成 ID 也不能重用。
                await events.start_task(receipt)  # Schedule 不再派生新的验证 Timer。
                assert (await timer.describe()).status is WorkflowExecutionStatus.COMPLETED
                await Replayer(workflows=[ReleaseVerificationTriggerWorkflow]).replay_workflow(
                    await timer.fetch_history()
                )


@time_skipping
@pytest.mark.asyncio
async def test_periodic_lost_commit_response_deduplicates(database: Database) -> None:
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ["TEST_TIME_SKIPPING_SERVER"]
    ) as env:
        config = settings(f"periodic-retry-{uuid4().hex}")
        events = EventActivities(database, config, env.client)
        calls = 0

        @activity.defn(name="event.persist")
        async def lost_response(batch: EventBatch) -> list[EventReceipt]:
            nonlocal calls
            result = await events.persist(batch)
            calls += 1
            if calls == 1:
                raise ApplicationError("模拟提交后丢响应")
            return result

        async with Worker(
            env.client,
            task_queue=config.temporal_config.task_queue,
            workflows=[PeriodicTriggerWorkflow, EventIngestionWorkflow],
            activities=[lost_response, events.start_task],
        ):
            handle = await env.client.start_workflow(
                PeriodicTriggerWorkflow.run,
                PeriodicInput("hourly-capacity", "retry-test"),
                id=f"periodic-retry-{uuid4().hex}",
                task_queue=config.temporal_config.task_queue,
            )
            receipt = await handle.result()
            assert receipt.duplicate and calls == 2
            async with database.session() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OpsEvent)
                        .where(OpsEvent.id == UUID(receipt.event_id))
                    )
                    == 1
                )


@time_skipping
@pytest.mark.asyncio
async def test_late_release_immediate_and_non_release_never_starts_timer(
    database: Database,
) -> None:
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.environ["TEST_TIME_SKIPPING_SERVER"]
    ) as env:
        config = settings(f"late-release-{uuid4().hex}")
        event_id = str(uuid4())
        old = await env.get_current_time() - timedelta(hours=1)
        async with create_worker(env.client, database, config):
            handle = await env.client.start_workflow(
                ReleaseVerificationTriggerWorkflow.run,
                ReleaseVerificationInput(event_id, "late-release", old),
                id=f"late-release-{event_id}",
                task_queue=config.temporal_config.task_queue,
            )
            receipt = await handle.result()
            await verify_receipt(database, receipt, old + timedelta(seconds=600))
