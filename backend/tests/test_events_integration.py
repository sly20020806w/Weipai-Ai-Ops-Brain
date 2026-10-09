"""Step 21 本地 PostgreSQL/Temporal 与全 Fake 事件接入验收。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import Request
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Replayer, Worker

from app.api.events import get_event_gateway
from app.api.main import create_app
from app.config import Settings, parse_database_url
from app.connectors.kubernetes.config import KubernetesConfig
from app.db.session import Database
from app.ledger.models import AuditRecord
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.config import TemporalConfig
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.triggers.activities import EventActivities
from app.triggers.config import TriggerConfig
from app.triggers.gateway import EventGateway
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventBatch, EventReceipt, NormalizedEvent, WatchInput
from app.triggers.service import EventService
from app.triggers.workflow import EventIngestionWorkflow, KubernetesEventWatchWorkflow
from app.verifier.placeholder import PlaceholderVerifier
from tests.database_support import get_test_database_url, migrate
from tests.test_events import SECRET, alert_body, signed

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-events.ps1"
)
temporal_test = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本地 Temporal 专项入口"
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


def sample_event(**updates: object) -> NormalizedEvent:
    value: dict[str, object] = dict(
        origin="manual",
        source=TaskSource.HUMAN,
        external_id=uuid4().hex,
        service_name="payment-service",
        title="事件接入测试",
        occurred_at=datetime(2026, 10, 1, 9, 30, tzinfo=UTC),
    )
    return NormalizedEvent.model_validate(value | updates)


async def persist(database: Database, events: list[NormalizedEvent]) -> list[EventReceipt]:
    async with database.session() as session, session.begin():
        return await EventService(session).accept(events)


@pytest.mark.asyncio
async def test_concurrent_dedup_one_event_task_history_and_audit(database: Database) -> None:
    value = sample_event()
    results = await asyncio.gather(*(persist(database, [value]) for _ in range(4)))
    assert len({receipt[0].event_id for receipt in results}) == 1
    assert sum(not receipt[0].duplicate for receipt in results) == 1
    async with database.session() as session:
        event = await session.get(OpsEvent, UUID(results[0][0].event_id))
        assert (
            event is not None and event.occurred_at.tzinfo is UTC and event.created_at.tzinfo is UTC
        )
        task = await session.get(AITask, event.task_id)
        assert (
            task is not None and task.source is TaskSource.HUMAN and task.status is TaskStatus.NEW
        )
        assert len(await TaskService(session).history(task.id)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(AuditRecord).where(AuditRecord.task_id == task.id)
            )
            == 1
        )
    changed = value.model_copy(update={"title": "重投摘要变化"})
    assert (await persist(database, [changed]))[0].duplicate


@pytest.mark.asyncio
async def test_multi_batch_lock_order_and_source_identity(database: Database) -> None:
    first, second = sample_event(), sample_event()
    results = await asyncio.gather(
        persist(database, [first, second]), persist(database, [second, first])
    )
    assert sum(not r.duplicate for rows in results for r in rows) == 2
    other = first.model_copy(update={"origin": "ops_platform", "source": TaskSource.TICKET})
    assert not (await persist(database, [other]))[0].duplicate
    assert len(await persist(database, [first, first])) == 1


@pytest.mark.asyncio
async def test_batch_rollback_removes_tasks_history_and_audit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = [
        sample_event(service_name=f"rollback-{uuid4().hex}"),
        sample_event(service_name=f"rollback-{uuid4().hex}"),
    ]
    original = TaskService.create
    calls = 0

    async def failing(self: TaskService, *, source: TaskSource, title: str, reason: str) -> AITask:
        nonlocal calls
        calls += 1
        task = await original(self, source=source, title=title, reason=reason)
        if calls == 2:
            raise RuntimeError("模拟批次中途失败")
        return task

    async with database.session() as session:
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (OpsEvent, AITask, TaskStatusHistory, AuditRecord)
        ]
    monkeypatch.setattr(TaskService, "create", failing)
    with pytest.raises(RuntimeError, match="中途失败"):
        await persist(database, events)
    async with database.session() as session:
        after = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (OpsEvent, AITask, TaskStatusHistory, AuditRecord)
        ]
    assert before == after


@pytest.mark.asyncio
async def test_event_constraints_and_utc(database: Database) -> None:
    value = sample_event()
    receipt = (await persist(database, [value]))[0]
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            session.add(
                OpsEvent(
                    fingerprint="bad",
                    origin="manual",
                    source="Human",
                    external_id="id",
                    service_name="payment-service",
                    title="重复关联",
                    occurred_at=datetime.now(UTC),
                    task_id=UUID(receipt.task_id),
                )
            )
            await session.flush()
    async with database.session() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name='ops_events' AND column_name='occurred_at'"
                )
            )
            == "timestamp with time zone"
        )


@pytest_asyncio.fixture
async def runtime(database: Database) -> AsyncIterator[tuple[Database, Client, Settings]]:
    config = TemporalConfig(
        address=os.environ["TEST_TEMPORAL_ADDRESS"], task_queue=f"events-test-{uuid4().hex}"
    )
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=get_test_database_url(),
        TEMPORAL_CONFIG=config,
        TRIGGER_CONFIG=TriggerConfig(
            webhook_secrets={"prometheus": SecretStr(SECRET)},
            response_timeout_seconds=30,
            watch_timeout_seconds=1,
        ),
        KUBERNETES_CONFIG=KubernetesConfig(
            cluster_name=f"fake-{uuid4().hex}", base_url="https://fake.example.invalid"
        ),
    )
    client = await Client.connect(config.address, namespace=config.namespace)
    yield database, client, settings


@temporal_test
@pytest.mark.asyncio
async def test_http_creates_alert_event_running_workflow_and_duplicate_401(
    runtime: tuple[Database, Client, Settings],
) -> None:
    database, client, settings = runtime
    app = create_app(settings)

    async def override(request: Request) -> EventGateway:
        return EventGateway(client, settings)

    app.dependency_overrides[get_event_gateway] = override
    body = alert_body(service=f"payment-{uuid4().hex}")
    async with create_worker(client, database, settings):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            first = await http.post("/webhooks/prometheus", content=body, headers=signed(body))
            assert first.status_code == 202
            receipt = first.json()["events"][0]
            handle = client.get_workflow_handle(receipt["workflow_id"])
            try:
                second = await http.post("/webhooks/prometheus", content=body, headers=signed(body))
                assert second.status_code == 202 and second.json()["events"][0]["duplicate"]
                assert second.json()["events"][0]["task_id"] == receipt["task_id"]
                invalid = await http.post(
                    "/webhooks/prometheus",
                    content=alert_body(service="bad-signature"),
                    headers=signed(body, secret="wrong"),
                )
                assert invalid.status_code == 401
                assert (await handle.describe()).status is WorkflowExecutionStatus.RUNNING
                async with database.session() as session:
                    records = list(
                        await session.scalars(
                            select(OpsEvent).where(OpsEvent.service_name == json_service(body))
                        )
                    )
                    assert len(records) == 1
                    task = await session.get(AITask, UUID(receipt["task_id"]))
                    assert task is not None and task.source is TaskSource.ALERT
                    assert not await session.scalar(
                        select(OpsEvent).where(OpsEvent.service_name == "bad-signature")
                    )
            finally:
                await handle.terminate("验收清理")


def json_service(body: bytes) -> str:
    import json

    return str(json.loads(body)["alerts"][0]["labels"]["service"])


@temporal_test
@pytest.mark.asyncio
async def test_temporal_lost_commit_and_start_responses_retry_and_replay(
    runtime: tuple[Database, Client, Settings],
) -> None:
    database, client, settings = runtime
    events = EventActivities(database, settings, client)
    tasks = TaskActivities(TaskActivityStore(database))
    verifier = PlaceholderVerifier(TaskActivityStore(database), app_env="test")
    persist_calls, start_calls = 0, 0

    @activity.defn(name="event.persist")
    async def lost_persist(batch: EventBatch) -> list[EventReceipt]:
        nonlocal persist_calls
        result = await events.persist(batch)
        persist_calls += 1
        if persist_calls == 1:
            raise ApplicationError("模拟提交后丢响应")
        return result

    @activity.defn(name="event.start_task")
    async def lost_start(receipt: EventReceipt) -> None:
        nonlocal start_calls
        await events.start_task(receipt)
        start_calls += 1
        if start_calls == 1:
            raise ApplicationError("模拟派发后丢响应")

    worker = Worker(
        client,
        task_queue=settings.temporal_config.task_queue,
        workflows=[EventIngestionWorkflow, AITaskWorkflow],
        activities=[
            lost_persist,
            lost_start,
            tasks.load,
            tasks.transition,
            tasks.placeholder_stage,
            verifier.verify,
        ],
    )
    value = sample_event()
    async with worker:
        handle = await client.start_workflow(
            EventIngestionWorkflow.run,
            EventBatch([value.model_dump_json()]),
            id=f"ingest-retry-{uuid4()}",
            task_queue=settings.temporal_config.task_queue,
        )
        receipt = (await asyncio.wait_for(handle.result(), 30))[0]
        try:
            assert persist_calls == start_calls == 2 and receipt.duplicate
            await Replayer(workflows=[EventIngestionWorkflow]).replay_workflow(
                await handle.fetch_history()
            )
            assert (
                await client.get_workflow_handle(receipt.workflow_id).describe()
            ).status is WorkflowExecutionStatus.RUNNING
            async with database.session() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OpsEvent)
                        .where(OpsEvent.fingerprint == value.fingerprint)
                    )
                    == 1
                )
        finally:
            await client.get_workflow_handle(receipt.workflow_id).terminate("验收清理")


@temporal_test
@pytest.mark.asyncio
async def test_fake_watcher_creates_event_and_keeps_checkpoint_across_restart(
    runtime: tuple[Database, Client, Settings],
) -> None:
    database, client, settings = runtime
    assert settings.kubernetes_config is not None
    external_id = f"{settings.kubernetes_config.cluster_name}/payment/event-payment-backoff"
    task_id: UUID | None = None
    handle = await client.start_workflow(
        KubernetesEventWatchWorkflow.run,
        WatchInput("payment"),
        id=f"watch-test-{uuid4()}",
        task_queue=settings.temporal_config.task_queue,
    )
    initial_run_id = handle.first_execution_run_id
    assert initial_run_id is not None
    try:
        async with create_worker(client, database, settings):
            async with asyncio.timeout(30):
                while True:
                    async with database.session() as session:
                        event = await session.scalar(
                            select(OpsEvent).where(OpsEvent.external_id == external_id)
                        )
                        if event is not None:
                            task_id = event.task_id
                            assert (
                                event.source == "Alert" and event.service_name == "payment-service"
                            )
                            break
                    await asyncio.sleep(0.05)
            # 接入子 Workflow 完成后才能 ContinueAsNew 保存 cursor。
            async with asyncio.timeout(30):
                while (await handle.describe()).run_id == initial_run_id:
                    await asyncio.sleep(0.05)
        async with create_worker(client, database, settings):
            await asyncio.sleep(1.2)
            async with database.session() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OpsEvent)
                        .where(OpsEvent.external_id == external_id)
                    )
                    == 1
                )
        original = client.get_workflow_handle(handle.id, run_id=initial_run_id)
        await Replayer(workflows=[KubernetesEventWatchWorkflow]).replay_workflow(
            await original.fetch_history()
        )
    finally:
        await handle.terminate("验收清理")
        if task_id is not None:
            try:
                await client.get_workflow_handle(f"ai-task-{task_id}").terminate("验收清理")
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
