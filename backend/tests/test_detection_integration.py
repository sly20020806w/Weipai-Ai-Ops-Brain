"""独立本机 PostgreSQL + Temporal，测试不接触真实运维系统。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from temporalio import activity
from temporalio.client import Client, ScheduleActionExecutionStartWorkflow, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.config import Settings, parse_database_url
from app.connectors.kubernetes.fake import FakeKubernetesConnector, sample_snapshot
from app.connectors.observability.detection_fake import FakeDetectionPrometheusConnector
from app.db.session import Database
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.triggers.activities import EventActivities
from app.triggers.detection.activities import DetectionActivities
from app.triggers.detection.config import DetectionConfig, StateRule
from app.triggers.detection.detectors import detect_state, detect_trend
from app.triggers.detection.models import (
    DetectionBatch,
    DetectionCursor,
    DetectionInput,
    DetectionRequest,
    DetectionResult,
    Observation,
)
from app.triggers.detection.schedule import ensure_detection_schedule
from app.triggers.detection.service import DetectionService
from app.triggers.detection.workflow import StatePredictionWorkflow
from app.triggers.models import OpsEvent
from tests.database_support import get_test_database_url, migrate
from tests.test_detection import END, trend

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-detection.ps1"
)
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal 专项入口"
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


def unique(value: Observation) -> Observation:
    return value.model_copy(
        update={
            "detector_key": sha256(uuid4().bytes).hexdigest(),
            "service_name": f"probe-{uuid4().hex}",
        }
    )


async def persist(database: Database, values: list[Observation]) -> DetectionResult:
    async with database.session() as session, session.begin():
        return await DetectionService(session).persist(values, END.isoformat())


@pytest.mark.asyncio
async def test_state_prediction_evidence_and_exact_exhaustion(database: Database) -> None:
    state = detect_state(StateRule(), sample_snapshot().deployments[0], "fake", END)
    predicted = detect_trend(*trend((0.5, 0.6, 0.7)))
    assert state is not None and predicted is not None
    result = await persist(database, [unique(state), unique(predicted)])
    assert len(result.receipts) == 2 and all(not receipt.duplicate for receipt in result.receipts)
    async with database.session() as session:
        sources = set()
        for receipt in result.receipts:
            task = await session.get(AITask, UUID(receipt.task_id))
            event = await session.get(OpsEvent, UUID(receipt.event_id))
            assert task is not None and event is not None and task.status is TaskStatus.NEW
            assert event.occurred_at.tzinfo is UTC
            sources.add(task.source)
            evidence = await LedgerService(session).evidence_for_task(task.id)
            assert len(evidence) == 1 and evidence[0].source_reference
            if task.source is TaskSource.PREDICTION:
                summary = evidence[0].result_snapshot
                assert isinstance(summary, dict)
                assert (
                    summary["predicted_exhaustion_at"] == (END + timedelta(minutes=2)).isoformat()
                )
                assert "预计耗尽" in task.title
        assert sources == {TaskSource.STATE, TaskSource.PREDICTION}


@pytest.mark.asyncio
async def test_concurrency_continuing_recovery_old_data_and_recurrence(database: Database) -> None:
    value = detect_state(StateRule(), sample_snapshot().deployments[0], "fake", END)
    assert value is not None
    value = unique(value)
    results = await asyncio.gather(*(persist(database, [value]) for _ in range(6)))
    task_ids = {result.receipts[0].task_id for result in results}
    assert len(task_ids) == 1
    assert sum(not result.receipts[0].duplicate for result in results) == 1
    newer = value.model_copy(update={"observed_at": END + timedelta(minutes=1)})
    assert (await persist(database, [newer])).receipts[0].duplicate
    recovery = newer.model_copy(
        update={"observed_at": END + timedelta(minutes=2), "breached": False}
    )
    assert (await persist(database, [recovery])).receipts == []
    old_retry = await persist(database, [value])
    assert old_retry.receipts[0].task_id in task_ids and old_retry.receipts[0].duplicate
    assert (
        await persist(
            database, [value.model_copy(update={"observed_at": END - timedelta(minutes=1)})]
        )
    ).receipts == []
    recurrence = value.model_copy(update={"observed_at": END + timedelta(minutes=3)})
    second = (await persist(database, [recurrence])).receipts[0]
    assert second.task_id not in task_ids and not second.duplicate
    with pytest.raises(ValueError, match="冲突"):
        await persist(database, [recurrence.model_copy(update={"breached": False})])
    async with database.session() as session:
        cursor = await session.scalar(
            select(DetectionCursor).where(DetectionCursor.detector_key == value.detector_key)
        )
        assert cursor is not None and str(cursor.active_event_id) == second.event_id
        assert cursor.observed_at == recurrence.observed_at
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OpsEvent)
                .where(OpsEvent.service_name == value.service_name)
            )
            == 2
        )
        assert len(await LedgerService(session).evidence_for_task(UUID(next(iter(task_ids))))) == 1


@pytest.mark.asyncio
async def test_normal_data_has_no_event_task_or_evidence(database: Database) -> None:
    value = detect_trend(*trend((0.4, 0.4, 0.4)))
    assert value is not None and not value.breached
    value = unique(value)
    assert (await persist(database, [value])).receipts == []
    async with database.session() as session:
        assert (
            await session.scalar(
                select(OpsEvent).where(OpsEvent.service_name == value.service_name)
            )
            is None
        )
        cursor = await session.scalar(
            select(DetectionCursor).where(DetectionCursor.detector_key == value.detector_key)
        )
        assert cursor is not None and cursor.active_event_id is None


@pytest.mark.asyncio
async def test_evidence_failure_rolls_back_whole_detection(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = detect_trend(*trend((0.5, 0.6, 0.7)))
    assert value is not None
    value = unique(value)

    async def fail(*args: object, **kwargs: object) -> Evidence:
        raise RuntimeError("模拟证据写入失败")

    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(RuntimeError, match="证据写入"):
        await persist(database, [value])
    async with database.session() as session:
        assert (
            await session.scalar(
                select(OpsEvent).where(OpsEvent.service_name == value.service_name)
            )
            is None
        )
        assert (
            await session.scalar(
                select(DetectionCursor).where(DetectionCursor.detector_key == value.detector_key)
            )
            is None
        )


@local_temporal
@pytest.mark.asyncio
async def test_real_schedule_repeated_detection_running_task_and_replay(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.triggers.detection import activities

    monkeypatch.setattr(
        activities, "create_kubernetes_connector", lambda settings: FakeKubernetesConnector()
    )
    monkeypatch.setattr(
        activities,
        "create_prometheus_connector",
        lambda settings: FakeDetectionPrometheusConnector(),
    )
    token = uuid4().hex
    config = Settings(
        APP_ENV="test",
        DETECTION_CONFIG=DetectionConfig(schedule_id=f"detection-test-{token}"),
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"detection-{token}",
        },
    )
    client = await Client.connect(config.temporal_config.address, namespace="default")
    receipts = []
    assert await ensure_detection_schedule(
        client, config.detection_config, config.temporal_config.task_queue
    )
    handle = client.get_schedule_handle(config.detection_config.schedule_id)
    await handle.pause(note="隔离验收，只手动触发")
    try:
        async with create_worker(client, database, config):

            async def trigger() -> tuple[DetectionResult, str]:
                previous = (await handle.describe()).info.num_actions
                await handle.trigger()
                async with asyncio.timeout(30):
                    while (description := await handle.describe()).info.num_actions <= previous:
                        await asyncio.sleep(0.05)
                action = description.info.recent_actions[-1].action
                assert isinstance(action, ScheduleActionExecutionStartWorkflow)
                execution = client.get_workflow_handle(
                    action.workflow_id, result_type=DetectionResult
                )
                result = await asyncio.wait_for(execution.result(), 30)
                await Replayer(workflows=[StatePredictionWorkflow]).replay_workflow(
                    await execution.fetch_history()
                )
                return result, action.workflow_id

            first, _ = await trigger()
            receipts.extend(first.receipts)
            assert len(first.receipts) == 5
            second, _ = await trigger()
            assert {r.task_id for r in first.receipts} == {r.task_id for r in second.receipts}
            assert all(r.duplicate for r in second.receipts)
            for receipt in first.receipts:
                execution = client.get_workflow_handle(receipt.workflow_id)
                async with asyncio.timeout(30):
                    while True:
                        progress = await execution.query(AITaskWorkflow.progress)
                        if progress.task and progress.task.status is TaskStatus.WAITING_INFORMATION:
                            break
                        await asyncio.sleep(0.05)
                assert (await execution.describe()).status is WorkflowExecutionStatus.RUNNING
    finally:
        await handle.delete()
        for receipt in receipts:
            await client.get_workflow_handle(receipt.workflow_id).terminate("验收清理")


@local_temporal
@pytest.mark.asyncio
async def test_commit_response_loss_retry_and_worker_restart(database: Database) -> None:
    token = uuid4().hex
    config = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"detection-retry-{token}",
        },
    )
    client = await Client.connect(config.temporal_config.address, namespace="default")
    value = detect_trend(*trend((0.5, 0.6, 0.7)))
    assert value is not None
    value = unique(value)
    calls = 0
    collect_calls = 0
    committed = asyncio.Event()

    @activity.defn(name="detection.collect")
    async def collect_once(request: DetectionRequest) -> DetectionBatch:
        nonlocal collect_calls
        collect_calls += 1
        assert collect_calls == 1, "提交后重试不能重新查询变化中的源系统"
        return DetectionBatch(END.isoformat(), [value.model_dump_json()])

    instance = DetectionActivities(database, config)

    @activity.defn(name="detection.persist")
    async def lost_response(batch: DetectionBatch) -> DetectionResult:
        nonlocal calls
        result = await instance.persist(batch)
        calls += 1
        if calls == 1:
            # 在重试之前模拟另一轮确认恢复，旧批次仍要派发已接受的事件。
            await persist(
                database,
                [
                    value.model_copy(
                        update={"observed_at": END + timedelta(minutes=1), "breached": False}
                    )
                ],
            )
            committed.set()
            raise ApplicationError("模拟提交后丢响应")
        return result

    events = EventActivities(database, config, client)
    result: DetectionResult | None = None
    try:
        async with Worker(
            client,
            task_queue=config.temporal_config.task_queue,
            workflows=[StatePredictionWorkflow],
            activities=[collect_once, lost_response, events.start_task],
            max_cached_workflows=0,
        ):
            execution = await client.start_workflow(
                StatePredictionWorkflow.run,
                DetectionInput(),
                id=f"detect-retry-{token}",
                task_queue=config.temporal_config.task_queue,
            )
            await asyncio.wait_for(committed.wait(), 30)
        async with Worker(
            client,
            task_queue=config.temporal_config.task_queue,
            workflows=[StatePredictionWorkflow],
            activities=[collect_once, lost_response, events.start_task],
            max_cached_workflows=0,
        ):
            result = await asyncio.wait_for(execution.result(), 30)
            assert calls == 2 and collect_calls == 1 and result.receipts[0].duplicate
            await Replayer(workflows=[StatePredictionWorkflow]).replay_workflow(
                await execution.fetch_history()
            )
        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(OpsEvent)
                    .where(OpsEvent.service_name == value.service_name)
                )
                == 1
            )
            assert (
                len(
                    await LedgerService(session).evidence_for_task(UUID(result.receipts[0].task_id))
                )
                == 1
            )
    finally:
        if result:
            await client.get_workflow_handle(result.receipts[0].workflow_id).terminate("验收清理")


@pytest.mark.asyncio
async def test_activity_invalid_time_and_read_failure_no_partial_commit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.triggers.detection import activities

    instance = DetectionActivities(database, Settings(APP_ENV="test"))
    with pytest.raises(ApplicationError, match="时间无效"):
        await instance.evaluate(DetectionRequest("2026-10-06T01:00:00"))

    def failed_source(settings: Settings) -> FakeKubernetesConnector:
        raise RuntimeError("敏感响应不能进入历史")

    monkeypatch.setattr(activities, "create_kubernetes_connector", failed_source)
    with pytest.raises(ApplicationError, match="状态或趋势采集失败") as error:
        await instance.evaluate(DetectionRequest(END.isoformat()))
    assert "敏感" not in str(error.value)
