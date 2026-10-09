"""Step 18：独立本地库验证刷新、回滚、Dispatcher 和 Temporal 实际执行。"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from temporalio import activity
from temporalio.client import Client, ScheduleActionExecutionStartWorkflow
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.config import Settings, parse_database_url
from app.db.base import utc_now
from app.db.session import Database
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.config import DiscoveryConfig
from app.graph.discovery.models import (
    DiscoveryInput,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySnapshot,
)
from app.graph.discovery.schedule import ensure_discovery_schedule
from app.graph.discovery.service import persist_snapshot
from app.graph.discovery.sources import configured_sources
from app.graph.discovery.workflow import DiscoveryWorkflow
from app.graph.models import GraphEdge, GraphNode
from app.graph.service import GraphService
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyConfig, PolicyDecision, PolicyEnvironment, PolicyRule, RiskLevel
from app.tasks.service import TaskService
from app.tasks.states import TaskSource
from app.tools import DispatchMode, DispatchStatus, ToolDispatcher
from app.tools.graph import GraphQueries, register_graph_tools
from app.tools.registry import ToolRegistry
from tests.database_support import get_test_database_url, migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-discovery.ps1 使用独立临时库"
)


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    database = Database(parse_database_url(get_test_database_url()))
    try:
        yield database
    finally:
        await database.dispose()


async def refresh(database: Database, offset: int = 0) -> DiscoverySnapshot:
    end = utc_now() + timedelta(seconds=offset)
    async with AsyncExitStack() as stack:
        sources = await configured_sources(stack, Settings(APP_ENV="test"), end)
        snapshot = await sources.collect(end, 900)
    async with database.session() as session, session.begin():
        await persist_snapshot(GraphService(session), snapshot, end.isoformat())
    return snapshot


async def edge_state(
    database: Database, snapshot: DiscoverySnapshot | None = None
) -> dict[UUID, tuple[datetime, datetime, float]]:
    async with database.session() as session:
        nodes = {n.id: (n.kind, n.external_id) for n in await session.scalars(select(GraphNode))}
        observed = (
            {(e.origin.key, e.target.key, e.relation, e.source) for e in snapshot.relations}
            if snapshot is not None
            else None
        )
        return {
            e.id: (e.first_seen, e.last_seen, e.confidence)
            for e in await session.scalars(select(GraphEdge))
            if observed is None
            or (nodes[e.from_node_id], nodes[e.to_node_id], e.relation, e.source) in observed
        }


@pytest.mark.asyncio
async def test_second_refresh_no_duplicates_and_older_observations_do_not_regress(
    database: Database,
) -> None:
    snapshot = await refresh(database)
    before = await edge_state(database, snapshot)
    await refresh(database, 60)
    after = await edge_state(database, snapshot)
    assert before.keys() == after.keys()
    assert all(
        after[key][0] == before[key][0]
        and after[key][1] > before[key][1]
        and after[key][2] == before[key][2]
        for key in before
    )
    await refresh(database, -60)
    assert await edge_state(database, snapshot) == after
    async with database.session() as session:
        context = await GraphQueries(GraphService(session)).read("payment-service", 2)
    assert {n.kind for n in context.nodes} >= {
        "repository",
        "version",
        "cluster",
        "pod",
        "database",
        "cache",
        "topic",
        "service",
    }
    assert all(
        e.source
        and e.first_seen.tzinfo is UTC
        and e.last_seen.tzinfo is UTC
        and e.freshness_seconds >= 0
        for e in context.edges
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direction,expected",
    [
        ("upstream", {"payment-service", "checkout-service"}),
        ("downstream", {"payment-service", "payment-db"}),
        ("both", {"payment-service", "checkout-service", "payment-db"}),
    ],
)
async def test_dependency_directions_only_follow_calls(
    database: Database, direction: str, expected: set[str]
) -> None:
    await refresh(database)
    async with database.session() as session:
        context = await GraphQueries(GraphService(session)).read(
            "payment-service", 1, direction, dependencies=True
        )
    assert {n.external_id for n in context.nodes} == expected
    assert all(e.relation == "calls" and e.source == "arms" for e in context.edges)


@pytest.mark.asyncio
async def test_snapshot_transaction_rollback_preserves_existing_graph(database: Database) -> None:
    await refresh(database)
    before = await edge_state(database)
    async with database.session() as session:
        with pytest.raises(RuntimeError, match="rollback"):
            async with session.begin():
                graph = GraphService(session)
                await graph.upsert_node(kind="service", external_id="rolled-back", name="回滚测试")
                first = next(iter(await session.scalars(select(GraphEdge))))
                await graph.upsert_edge(
                    from_node_id=first.from_node_id,
                    to_node_id=first.to_node_id,
                    relation=first.relation,
                    source=first.source,
                    confidence=first.confidence,
                    observed_at=first.last_seen + timedelta(days=1),
                )
                raise RuntimeError("rollback")
        assert (
            await session.scalar(select(GraphNode).where(GraphNode.external_id == "rolled-back"))
            is None
        )
    assert await edge_state(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_service_context", "get_dependencies"])
async def test_graph_tool_ledger_policy_and_original_replay(database: Database, tool: str) -> None:
    await refresh(database)
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="图 Tool 验收", reason="Step 18"
        )
        registry = ToolRegistry()
        register_graph_tools(registry, GraphService(session))
        result = await ToolDispatcher(
            registry, PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
        ).dispatch(
            task_id=task.id,
            tool_name=tool,
            parameters={"service_name": "payment-service"},
            actor="fake-agent",
        )
    assert result.status is DispatchStatus.SUCCEEDED and result.evidence_id is not None
    await refresh(database, 120)
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
        registry = ToolRegistry()
        register_graph_tools(registry, GraphService(session))
        replay = await ToolDispatcher(
            registry, PolicyEngine(PolicyEnvironment.TEST), ledger
        ).dispatch(
            task_id=task.id,
            tool_name=tool,
            parameters={"service_name": "payment-service"},
            actor="replay",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=evidence.id,
            replay_before=evidence.collected_at + timedelta(seconds=1),
        )
        assert replay.status is DispatchStatus.REPLAYED and replay.result == result.result
        assert replay.evidence_id == result.evidence_id
        assert len(await ledger.evidence_for_task(task.id)) == 1


@pytest.mark.asyncio
async def test_policy_denial_and_missing_service_no_success_evidence(database: Database) -> None:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="拒绝图查询", reason="Step 18"
        )
        registry = ToolRegistry()
        register_graph_tools(registry, GraphService(session))
        ledger = LedgerService(session)
        policy = PolicyEngine(
            PolicyEnvironment.TEST,
            PolicyConfig(
                rules=(
                    PolicyRule(
                        id="deny-graph",
                        risk_levels=(RiskLevel.L0,),
                        decision=PolicyDecision.DENY,
                        reason="验收拒绝",
                        action_names=("get_service_context",),
                    ),
                )
            ),
        )
        denied = await ToolDispatcher(registry, policy, ledger).dispatch(
            task_id=task.id,
            tool_name="get_service_context",
            parameters={"service_name": "payment-service"},
            actor="AI",
        )
        assert denied.status is DispatchStatus.REJECTED
        missing = await ToolDispatcher(
            registry, PolicyEngine(PolicyEnvironment.TEST), ledger
        ).dispatch(
            task_id=task.id,
            tool_name="get_service_context",
            parameters={"service_name": "absent"},
            actor="AI",
        )
        assert missing.status is DispatchStatus.FAILED
        assert not await ledger.evidence_for_task(task.id)


@pytest.mark.skipif(not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本地 Temporal")
@pytest.mark.asyncio
async def test_actual_workflow_retry_replay_and_repeated_schedule(database: Database) -> None:
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"], namespace="default")
    queue = f"discovery-test-{uuid4().hex}"
    activities = DiscoveryActivities(database, Settings(APP_ENV="test"))
    calls = 0

    @activity.defn(name="discovery.refresh")
    async def lost_response(request: DiscoveryRequest) -> DiscoveryResult:
        nonlocal calls
        result = await activities.refresh(request)
        calls += 1
        if calls == 1:
            raise ApplicationError("模拟提交后丢失响应")
        return result

    schedule_config = DiscoveryConfig(
        schedule_id=f"discovery-test-{uuid4().hex}", interval_seconds=2
    )
    handle = client.get_schedule_handle(schedule_config.schedule_id)
    try:
        async with Worker(
            client, task_queue=queue, workflows=[DiscoveryWorkflow], activities=[lost_response]
        ):
            execution = await client.start_workflow(
                DiscoveryWorkflow.run,
                DiscoveryInput(),
                id=f"discovery-test-{uuid4().hex}",
                task_queue=queue,
            )
            result = await asyncio.wait_for(execution.result(), 30)
            assert result.node_count >= 20 and calls == 2
            history = await execution.fetch_history()
            await Replayer(workflows=[DiscoveryWorkflow]).replay_workflow(history)
            assert await ensure_discovery_schedule(client, schedule_config, queue)
            assert not await ensure_discovery_schedule(client, schedule_config, queue)
            # 等待真实 Schedule 到点，不使用业务轮询调度器。
            async with asyncio.timeout(20):
                while (await handle.describe()).info.num_actions < 2:
                    await asyncio.sleep(0.1)
            description = await handle.describe()
            await handle.pause(note="测试已获得两次周期触发")
            for recent in description.info.recent_actions[-2:]:
                if isinstance(recent.action, ScheduleActionExecutionStartWorkflow):
                    scheduled = client.get_workflow_handle(
                        recent.action.workflow_id, result_type=DiscoveryResult
                    )
                    scheduled_result = await asyncio.wait_for(scheduled.result(), 30)
                    assert scheduled_result.node_count == result.node_count
            assert (await handle.describe()).schedule.state.paused
    finally:
        await handle.delete()
