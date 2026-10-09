"""Step 17：本地 Temporal 测试环境与独立 PostgreSQL 库的闭环验收。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from app.config import Settings, parse_database_url
from app.db.session import Database
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.config import TemporalConfig
from app.tasks.models import AITask
from app.tasks.safety.activities import SafetyActivities
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow, validate_placeholder_settings
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import (
    HumanResponse,
    TaskSnapshot,
    TransitionRequest,
    WorkflowInput,
    WorkflowProgress,
)
from app.verifier.placeholder import PlaceholderVerifier
from tests.database_support import get_test_database_url, migrate

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_TEMPORAL_ADDRESS")),
    reason="执行 check-workflow.ps1 进行本地 Temporal + PostgreSQL 专项验收",
)


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def runtime(
    migrated_schema: None,
) -> AsyncIterator[tuple[WorkflowEnvironment, Database, Settings]]:
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG=TemporalConfig(
            address=os.environ["TEST_TEMPORAL_ADDRESS"],
            namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default"),
            task_queue=f"weipai-workflow-test-{uuid4().hex}",
        ),
    )
    validate_placeholder_settings(settings)
    client = await Client.connect(
        settings.temporal_config.address, namespace=settings.temporal_config.namespace
    )
    environment = WorkflowEnvironment.from_client(client)
    database = Database(parse_database_url(get_test_database_url()))
    try:
        yield environment, database, settings
    finally:
        await database.dispose()
        await environment.shutdown()


async def task_input(database: Database) -> WorkflowInput:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="Temporal 占位验收", reason="Step 17 本地测试环境"
        )
    return WorkflowInput(str(task.id))


async def wait_at(
    handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress], status: TaskStatus
) -> TaskSnapshot:
    # 仅测试观察同步，不是业务调度。生产等待由 Workflow 的持久化信号/定时器完成。
    async with asyncio.timeout(15):
        while True:
            progress = await handle.query(AITaskWorkflow.progress)
            if progress.task is not None and progress.task.status is status:
                return progress.task
            await asyncio.sleep(0.02)


async def assert_consistent(
    environment: WorkflowEnvironment,
    database: Database,
    handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress],
    result: WorkflowProgress,
) -> None:
    assert result.task is not None
    task_id = UUID(result.task.task_id)
    async with database.session() as session:
        task = await session.get(AITask, task_id)
        assert task is not None
        assert (task.status, task.status_version) == (result.task.status, result.task.version)
        history = await TaskService(session).history(task_id)
        assert [(item.to_status, item.sequence) for item in history] == [
            (item.status, item.version) for item in result.history
        ]
        assert all(item.changed_at.tzinfo is UTC and item.reason for item in history)
        audits = [
            a
            for a in await LedgerService(session).audits_for_task(task_id)
            if a.event_type is AuditEventType.STATE_TRANSITION
        ]
        assert len(audits) == len(history)
        assert all(item.event_type is AuditEventType.STATE_TRANSITION for item in audits)
        for entry in history:
            if entry.to_status is TaskStatus.RESOLVED:
                assert entry.actor.value == "verifier"
    temporal_history = await handle.fetch_history()
    persisted: list[TaskSnapshot] = []
    for event in temporal_history.events:
        if event.HasField("activity_task_completed_event_attributes"):
            payloads = event.activity_task_completed_event_attributes.result.payloads
            if payloads:
                values = await environment.client.data_converter.decode(payloads)
                if isinstance(values[0], dict) and "status" in values[0]:
                    decoded = await environment.client.data_converter.decode(
                        payloads, [TaskSnapshot]
                    )
                    persisted.append(decoded[0])
    assert persisted == result.history
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(temporal_history)


@pytest.mark.asyncio
async def test_closed_history_and_replay(
    runtime: tuple[WorkflowEnvironment, Database, Settings],
) -> None:
    environment, database, settings = runtime
    value = await task_input(database)
    async with create_worker(environment.client, database, settings):
        handle = await start_task_workflow(
            environment.client, value, task_queue=settings.temporal_config.task_queue
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.task is not None and result.task.status is TaskStatus.CLOSED
    assert [item.status for item in result.history] == [
        TaskStatus.NEW,
        TaskStatus.CONTEXT_BUILDING,
        TaskStatus.RUNBOOK_MATCHING,
        TaskStatus.INVESTIGATING,
        TaskStatus.RCA,
        TaskStatus.PLANNING,
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
        TaskStatus.RESOLVED,
        TaskStatus.LEARNING,
        TaskStatus.CLOSED,
    ]
    await assert_consistent(environment, database, handle, result)
    with pytest.raises(WorkflowAlreadyStartedError):
        await start_task_workflow(
            environment.client, value, task_queue=settings.temporal_config.task_queue
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "waiting",
    [TaskStatus.WAITING_INFORMATION, TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_APPROVAL],
)
async def test_signal_resumes_after_worker_restart(
    runtime: tuple[WorkflowEnvironment, Database, Settings], waiting: TaskStatus
) -> None:
    environment, database, settings = runtime
    value = await task_input(database)
    value = WorkflowInput(value.task_id, waits=[waiting], human_timeout_seconds=60)
    async with create_worker(environment.client, database, settings):
        handle = await start_task_workflow(
            environment.client, value, task_queue=settings.temporal_config.task_queue
        )
        snapshot = await wait_at(handle, waiting)
        await handle.signal(
            AITaskWorkflow.human_response, HumanResponse(waiting, snapshot.version - 1, True)
        )
        assert (await handle.query(AITaskWorkflow.progress)).task == snapshot
    # Worker 停止期间信号由 Temporal 接收；新 Worker 回放后恢复。
    await handle.signal(
        AITaskWorkflow.human_response, HumanResponse(waiting, snapshot.version, True)
    )
    async with create_worker(environment.client, database, settings):
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.task is not None and result.task.status is TaskStatus.CLOSED
    await assert_consistent(environment, database, handle, result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "waiting",
    [TaskStatus.WAITING_INFORMATION, TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_APPROVAL],
)
async def test_wait_timeout_escalates_without_execution(
    runtime: tuple[WorkflowEnvironment, Database, Settings], waiting: TaskStatus
) -> None:
    environment, database, settings = runtime
    value = await task_input(database)
    value = WorkflowInput(value.task_id, waits=[waiting], human_timeout_seconds=0.15)
    async with create_worker(environment.client, database, settings):
        handle = await start_task_workflow(
            environment.client, value, task_queue=settings.temporal_config.task_queue
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.task is not None and result.task.status is TaskStatus.ESCALATED
    assert TaskStatus.EXECUTING not in [item.status for item in result.history]
    await assert_consistent(environment, database, handle, result)


@pytest.mark.asyncio
async def test_approval_rejection_does_not_execute(
    runtime: tuple[WorkflowEnvironment, Database, Settings],
) -> None:
    environment, database, settings = runtime
    value = await task_input(database)
    value = WorkflowInput(value.task_id, waits=[TaskStatus.WAITING_APPROVAL])
    async with create_worker(environment.client, database, settings):
        handle = await start_task_workflow(
            environment.client, value, task_queue=settings.temporal_config.task_queue
        )
        snapshot = await wait_at(handle, TaskStatus.WAITING_APPROVAL)
        await handle.signal(
            AITaskWorkflow.human_response,
            HumanResponse(snapshot.status, snapshot.version, False),
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.task is not None and result.task.status is TaskStatus.ESCALATED
    assert TaskStatus.EXECUTING not in [item.status for item in result.history]
    await assert_consistent(environment, database, handle, result)


@pytest.mark.asyncio
async def test_temporal_retry_after_commit_does_not_duplicate_history(
    runtime: tuple[WorkflowEnvironment, Database, Settings],
) -> None:
    environment, database, settings = runtime
    store = TaskActivityStore(database)
    activities = TaskActivities(store)
    verifier = PlaceholderVerifier(store, app_env="test")
    calls = 0

    @activity.defn(name="task.transition")
    async def lost_response(request: TransitionRequest) -> TaskSnapshot:
        nonlocal calls
        result = await activities.transition(request)
        if request.task.version == 0:
            calls += 1
            if calls == 1:
                raise ApplicationError("模拟已提交但响应丢失")
        return result

    async with Worker(
        environment.client,
        task_queue=settings.temporal_config.task_queue,
        workflows=[AITaskWorkflow],
        activities=[activities.load, lost_response, activities.placeholder_stage, verifier.verify],
    ):
        handle = await start_task_workflow(
            environment.client,
            await task_input(database),
            task_queue=settings.temporal_config.task_queue,
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert calls == 2
    assert result.task is not None and result.task.status is TaskStatus.CLOSED
    await assert_consistent(environment, database, handle, result)


@pytest.mark.asyncio
async def test_activity_retry_exhaustion_escalates(
    runtime: tuple[WorkflowEnvironment, Database, Settings],
) -> None:
    environment, database, settings = runtime
    activities = TaskActivities(TaskActivityStore(database))
    calls = 0

    @activity.defn(name="task.placeholder_stage")
    async def failed_stage(task: TaskSnapshot) -> None:
        nonlocal calls
        calls += 1
        raise ApplicationError("模拟阶段失败")

    async with Worker(
        environment.client,
        task_queue=settings.temporal_config.task_queue,
        workflows=[AITaskWorkflow],
        activities=[
            activities.load,
            activities.transition,
            failed_stage,
            SafetyActivities(database, settings).check,
            SafetyActivities(database, settings).notify,
        ],
    ):
        value = await task_input(database)
        value = WorkflowInput(value.task_id, activity_max_attempts=2)
        handle = await start_task_workflow(
            environment.client, value, task_queue=settings.temporal_config.task_queue
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert calls == 2
    assert result.task is not None and result.task.status is TaskStatus.ESCALATED
    await assert_consistent(environment, database, handle, result)
