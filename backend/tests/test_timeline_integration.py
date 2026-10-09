"""Step 19 本地 PostgreSQL/Temporal：事务、并发去重、证据与历史回放。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.config import Settings, parse_database_url
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.models import ChangeEvent
from app.graph.changes.schemas import ChangeSnapshot, TimelineInput, TimelineRequest, TimelineResult
from app.graph.changes.service import ChangeConflict, TimelineService
from app.graph.changes.workflow import ChangeTimelineWorkflow
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyConfig, PolicyDecision, PolicyEnvironment, PolicyRule, RiskLevel
from app.tasks.service import TaskService
from app.tasks.states import TaskSource
from app.tools import DispatchMode, DispatchStatus, ToolDispatcher
from app.tools.models import JsonObject
from app.tools.registry import ToolRegistry
from app.tools.timeline import register_timeline_tools
from tests.database_support import get_test_database_url, migrate
from tests.test_timeline import CORE, END, START, snapshot

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-timeline.ps1"
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


async def persist(database: Database, value: ChangeSnapshot) -> TimelineResult:
    async with database.session() as session, session.begin():
        return await TimelineService(session).persist(value)


@pytest.mark.asyncio
async def test_repeat_and_concurrent_collection_preserve_ids_and_utc(database: Database) -> None:
    original = await snapshot()
    # 隔离服务，回归总入口的其他用例不会影响计数。
    value = ChangeSnapshot(
        service_name="dedup-service",
        start=START,
        end=END,
        events=tuple(
            e.model_copy(update={"service_name": "dedup-service"}) for e in original.events
        ),
    )
    results = await asyncio.gather(persist(database, value), persist(database, value))
    assert sum(r.inserted_count for r in results) == 8
    async with database.session() as session:
        before = await TimelineService(session).recent("dedup-service", START, END)
    assert (await persist(database, value)).inserted_count == 0
    async with database.session() as session:
        after = await TimelineService(session).recent("dedup-service", START, END)
    assert before == after and len(after) == 8
    assert all(e.occurred_at.tzinfo is UTC and e.collected_at.tzinfo is UTC for e in after)
    assert [e.kind for e in after if e.kind in CORE] == CORE


@pytest.mark.asyncio
async def test_conflicting_reference_rolls_back_entire_snapshot(database: Database) -> None:
    value = await snapshot()
    await persist(database, value)
    first = value.events[0]
    new = first.model_copy(update={"source_ref": "aaa-new-rollback-reference"})
    conflicting = first.model_copy(update={"occurred_at": first.occurred_at + timedelta(seconds=1)})
    broken = ChangeSnapshot(
        service_name=value.service_name, start=START, end=END, events=(new, conflicting)
    )
    with pytest.raises(ChangeConflict):
        await persist(database, broken)
    async with database.session() as session:
        assert not await session.scalar(
            select(ChangeEvent).where(ChangeEvent.source_ref == new.source_ref)
        )
        assert len(await TimelineService(session).recent("payment-service", START, END)) == 8


@pytest.mark.asyncio
async def test_half_open_window_and_service_scope(database: Database) -> None:
    await persist(database, await snapshot())
    async with database.session() as session:
        service = TimelineService(session)
        assert not await service.recent("absent-service", START, END)
        assert not await service.recent("payment-service", END, END + timedelta(hours=1))
        assert [
            e.kind
            for e in await service.recent("payment-service", START, START + timedelta(minutes=5))
        ] == ["Commit"]
        assert [
            e.kind
            for e in await service.recent(
                "payment-service", START + timedelta(minutes=5), START + timedelta(minutes=6)
            )
        ] == ["Merge"]


@pytest.mark.asyncio
async def test_database_constraints_and_timezone_type(database: Database) -> None:
    async with database.session() as session:
        column_type = await session.scalar(
            text(
                "SELECT data_type FROM information_schema.columns WHERE table_name='change_events' "
                "AND column_name='occurred_at'"
            )
        )
        assert column_type == "timestamp with time zone"
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            session.add(
                ChangeEvent(
                    service_name="invalid",
                    source="argocd",
                    kind="Deploy",
                    source_ref="wrong",
                    occurred_at=START,
                )
            )


@pytest.mark.asyncio
async def test_dispatcher_one_evidence_audit_and_replay_without_query(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await persist(database, await snapshot())
    parameters: JsonObject = {
        "service_name": "payment-service",
        "lookback_seconds": 3600,
        "end": END.isoformat(),
    }
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="变更查询验收", reason="Step 19"
        )
        registry = ToolRegistry()
        register_timeline_tools(registry, TimelineService(session))
        result = await ToolDispatcher(
            registry, PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
        ).dispatch(
            task_id=task.id,
            tool_name="get_recent_changes",
            parameters=parameters,
            actor="fake-agent",
        )
        assert result.status is DispatchStatus.SUCCEEDED and result.evidence_id is not None
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        assert len(await ledger.evidence_for_task(task.id)) == 1
        assert (
            len(
                [
                    a
                    for a in await ledger.audits_for_task(task.id)
                    if a.event_type is AuditEventType.TOOL_CALL
                ]
            )
            == 1
        )
        evidence = await ledger.get_evidence(result.evidence_id)

        async def no_query(*args: object, **kwargs: object) -> None:
            raise AssertionError("Replay 不能执行实时查询")

        monkeypatch.setattr(TimelineService, "recent", no_query)
        registry = ToolRegistry()
        register_timeline_tools(registry, TimelineService(session))
        replay = await ToolDispatcher(
            registry, PolicyEngine(PolicyEnvironment.TEST), ledger
        ).dispatch(
            task_id=task.id,
            tool_name="get_recent_changes",
            parameters=parameters,
            actor="replay",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=evidence.id,
            replay_before=evidence.collected_at + timedelta(seconds=1),
        )
        assert replay.status is DispatchStatus.REPLAYED and replay.result == result.result
        assert (
            replay.evidence_id == evidence.id and len(await ledger.evidence_for_task(task.id)) == 1
        )


@pytest.mark.asyncio
async def test_policy_denial_does_not_execute_or_generate_evidence(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_query(*args: object, **kwargs: object) -> None:
        raise AssertionError("Policy 拒绝不能执行查询")

    monkeypatch.setattr(TimelineService, "recent", no_query)
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="拒绝变更查询", reason="Step 19"
        )
        ledger = LedgerService(session)
        registry = ToolRegistry()
        register_timeline_tools(registry, TimelineService(session))
        policy = PolicyEngine(
            PolicyEnvironment.TEST,
            PolicyConfig(
                rules=(
                    PolicyRule(
                        id="deny-timeline",
                        risk_levels=(RiskLevel.L0,),
                        decision=PolicyDecision.DENY,
                        reason="测试拒绝",
                        action_names=("get_recent_changes",),
                    ),
                )
            ),
        )
        result = await ToolDispatcher(registry, policy, ledger).dispatch(
            task_id=task.id,
            tool_name="get_recent_changes",
            parameters={"service_name": "payment-service"},
            actor="AI",
        )
        assert result.status is DispatchStatus.REJECTED and not await ledger.evidence_for_task(
            task.id
        )


@pytest.mark.skipif(not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本地 Temporal")
@pytest.mark.asyncio
async def test_actual_workflow_lost_response_retry_stable_window_and_history_replay(
    database: Database,
) -> None:
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"], namespace="default")
    queue = f"timeline-test-{uuid4().hex}"
    implementation = TimelineActivities(database, Settings(APP_ENV="test"))
    calls: list[TimelineRequest] = []

    @activity.defn(name="timeline.collect")
    async def lose_response(request: TimelineRequest) -> TimelineResult:
        result = await implementation.collect(request)
        calls.append(request)
        if len(calls) == 1:
            raise ApplicationError("模拟提交后丢响应")
        return result

    async with Worker(
        client, task_queue=queue, workflows=[ChangeTimelineWorkflow], activities=[lose_response]
    ):
        handle = await client.start_workflow(
            ChangeTimelineWorkflow.run,
            TimelineInput(end=END.isoformat()),
            id=f"timeline-test-{uuid4().hex}",
            task_queue=queue,
        )
        result = await asyncio.wait_for(handle.result(), 30)
        assert result.event_count == 8 and result.inserted_count == 0
        assert len(calls) == 2 and calls[0] == calls[1]
        await Replayer(workflows=[ChangeTimelineWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
        second = await client.execute_workflow(
            ChangeTimelineWorkflow.run,
            TimelineInput(end=END.isoformat()),
            id=f"timeline-test-{uuid4().hex}",
            task_queue=queue,
        )
        assert second.inserted_count == 0
    async with database.session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ChangeEvent)
                .where(ChangeEvent.service_name == "payment-service")
            )
            == 8
        )
