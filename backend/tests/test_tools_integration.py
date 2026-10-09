"""Step 9：独立临时 PostgreSQL 库中的真实证据、审计和事务原子性。"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.config import parse_database_url
from app.connectors.changes.fake import (
    FakeArgoCDConnector,
    FakeCIConnector,
    FakeConfigCenterConnector,
    FakeGitConnector,
)
from app.connectors.cloud.fake import FakeCloudConnector
from app.connectors.kubernetes.fake import FakeKubernetesConnector
from app.connectors.observability.fake import (
    FakeARMSConnector,
    FakePrometheusConnector,
    FakeSLSConnector,
)
from app.db.base import utc_now
from app.db.session import Database
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyEnvironment, RiskLevel
from app.tasks.service import TaskNotFound, TaskService
from app.tasks.states import TaskSource
from app.tools import DispatchMode, DispatchStatus, ToolDispatcher
from app.tools.changes import register_change_tools
from app.tools.cloud import register_cloud_tools
from app.tools.kubernetes import register_kubernetes_tools
from app.tools.models import JsonObject
from app.tools.observability import register_observability_tools
from app.tools.registry import ToolRegistry
from tests.database_support import get_test_database_url, migrate
from tests.test_tools import FakeTool, registry_for

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-db.ps1 进行本地 PostgreSQL 验收"
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


async def create_task(database: Database) -> UUID:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="Tool Fake 验收", reason="Step 9 独立临时库"
        )
        return task.id


@pytest.mark.asyncio
async def test_cloud_evidence_commit_and_replay(database: Database) -> None:
    """Step 15：云资源快照跨会话持久化，关闭来源后复用 Evidence ID 回放。"""
    task_id = await create_task(database)
    parameters: JsonObject = {
        "service_name": "payment-service",
        "start": "2026-10-01T01:00:00Z",
        "end": "2026-10-01T01:10:00Z",
    }
    async with FakeCloudConnector() as reader:
        registry = ToolRegistry()
        register_cloud_tools(registry, reader)
        async with database.session() as session, session.begin():
            live = await ToolDispatcher(
                registry, PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
            ).dispatch(
                task_id=task_id,
                tool_name="get_cloud_resources",
                parameters=parameters,
                actor="fake-agent",
            )
        assert live.status is DispatchStatus.SUCCEEDED and live.evidence_id is not None
        await reader.aclose()
        async with database.session() as session, session.begin():
            ledger = LedgerService(session)
            evidence = await ledger.evidence_for_task(task_id)
            audits = [
                a
                for a in await ledger.audits_for_task(task_id)
                if a.event_type is AuditEventType.TOOL_CALL
            ]
            assert len(evidence) == len(audits) == 1
            assert evidence[0].id == live.evidence_id == audits[0].evidence_id
            assert evidence[0].source_tool == "get_cloud_resources"
            assert evidence[0].parameters == parameters
            assert evidence[0].result_snapshot == live.result
            assert evidence[0].collected_at.tzinfo is UTC
            replay = await ToolDispatcher(
                registry, PolicyEngine(PolicyEnvironment.TEST), ledger
            ).dispatch(
                task_id=task_id,
                tool_name="get_cloud_resources",
                parameters=parameters,
                actor="replay-agent",
                mode=DispatchMode.REPLAY,
                replay_evidence_id=live.evidence_id,
                replay_before=utc_now(),
            )
            assert replay.status is DispatchStatus.REPLAYED and replay.result == live.result
            assert replay.evidence_id == live.evidence_id
        async with database.session() as session:
            ledger = LedgerService(session)
            assert len(await ledger.evidence_for_task(task_id)) == 1
            audits = [
                a
                for a in await ledger.audits_for_task(task_id)
                if a.event_type is AuditEventType.TOOL_CALL
            ]
            assert [audit.outcome for audit in audits] == ["succeeded", "replayed"]


@pytest.mark.asyncio
async def test_l0_persists_exact_pair_cross_session(database: Database) -> None:
    task_id = await create_task(database)
    fake = FakeTool()
    async with database.session() as session, session.begin():
        result = await ToolDispatcher(
            registry_for(fake), PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
        ).dispatch(
            task_id=task_id,
            tool_name="get_service_context",
            parameters={"service": "payment-service"},
            actor="fake-agent",
        )
    async with database.session() as session:
        ledger = LedgerService(session)
        evidence = await ledger.evidence_for_task(task_id)
        audits = [
            audit
            for audit in await ledger.audits_for_task(task_id)
            if audit.event_type is AuditEventType.TOOL_CALL
        ]
        assert len(evidence) == len(audits) == len(fake.calls) == 1
        assert result.status is DispatchStatus.SUCCEEDED
        assert evidence[0].id == result.evidence_id == audits[0].evidence_id
        assert evidence[0].parameters == {"service": "payment-service", "limit": 2}
        assert evidence[0].result_snapshot == result.result
        assert evidence[0].collected_at.tzinfo is UTC and audits[0].occurred_at.tzinfo is UTC
        assert audits[0].actor == "fake-agent" and audits[0].outcome == "succeeded"


@pytest.mark.asyncio
async def test_replay_after_commit_never_invokes_handler(database: Database) -> None:
    task_id = await create_task(database)
    async with database.session() as session, session.begin():
        live = await ToolDispatcher(
            registry_for(FakeTool()), PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
        ).dispatch(
            task_id=task_id,
            tool_name="get_service_context",
            parameters={"service": "payment-service"},
            actor="fake-agent",
        )
    replay_fake = FakeTool()
    async with database.session() as session, session.begin():
        replay = await ToolDispatcher(
            registry_for(replay_fake), PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
        ).dispatch(
            task_id=task_id,
            tool_name="get_service_context",
            parameters={"service": "payment-service"},
            actor="replay-agent",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=live.evidence_id,
            replay_before=utc_now(),
        )
    assert replay.status is DispatchStatus.REPLAYED
    assert replay.result == live.result and replay.evidence_id == live.evidence_id
    assert replay_fake.calls == []
    async with database.session() as session:
        ledger = LedgerService(session)
        assert len(await ledger.evidence_for_task(task_id)) == 1
        audits = [
            audit
            for audit in await ledger.audits_for_task(task_id)
            if audit.event_type is AuditEventType.TOOL_CALL
        ]
        assert [audit.outcome for audit in audits] == ["succeeded", "replayed"]


@pytest.mark.asyncio
async def test_approval_rejection_is_audited_without_evidence(database: Database) -> None:
    task_id = await create_task(database)
    fake = FakeTool()
    async with database.session() as session, session.begin():
        result = await ToolDispatcher(
            registry_for(fake, RiskLevel.L3),
            PolicyEngine(PolicyEnvironment.PRODUCTION),
            LedgerService(session),
        ).dispatch(
            task_id=task_id,
            tool_name="get_service_context",
            parameters={"service": "payment-service"},
            actor="fake-agent",
        )
    assert result.error_code == "approval_required" and fake.calls == []
    async with database.session() as session:
        ledger = LedgerService(session)
        assert await ledger.evidence_for_task(task_id) == []
        audits = await ledger.audits_for_task(task_id)
        assert len(audits) == 2  # task.create 与拒绝的 Tool 调用
        assert audits[-1].outcome == "rejected" and audits[-1].evidence_id is None


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_evidence_savepoint(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = await create_task(database)
    fake = FakeTool()
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        original = ledger.append_audit

        async def fail_audit(**kwargs: object) -> AuditRecord:
            raise RuntimeError("Fake audit storage failure")

        monkeypatch.setattr(ledger, "append_audit", fail_audit)
        with pytest.raises(RuntimeError, match="Fake audit"):
            await ToolDispatcher(
                registry_for(fake), PolicyEngine(PolicyEnvironment.TEST), ledger
            ).dispatch(
                task_id=task_id,
                tool_name="get_service_context",
                parameters={"service": "payment-service"},
                actor="fake-agent",
            )
        assert await ledger.evidence_for_task(task_id) == []
        # 调用方即使捕获错误并提交外层事务，也不会提交无审计的证据。
        monkeypatch.setattr(ledger, "append_audit", original)
    async with database.session() as session:
        assert await LedgerService(session).evidence_for_task(task_id) == []
        assert len(await LedgerService(session).audits_for_task(task_id)) == 1


@pytest.mark.asyncio
async def test_outer_rollback_removes_both_records(database: Database) -> None:
    task_id = await create_task(database)
    evidence_id: UUID | None = None
    audit_id: UUID | None = None
    with pytest.raises(RuntimeError, match="Fake caller"):
        async with database.session() as session, session.begin():
            result = await ToolDispatcher(
                registry_for(FakeTool()),
                PolicyEngine(PolicyEnvironment.TEST),
                LedgerService(session),
            ).dispatch(
                task_id=task_id,
                tool_name="get_service_context",
                parameters={"service": "payment-service"},
                actor="fake-agent",
            )
            evidence_id, audit_id = result.evidence_id, result.audit_id
            raise RuntimeError("Fake caller rollback")
    assert evidence_id is not None and audit_id is not None
    async with database.session() as session:
        assert await session.get(Evidence, evidence_id) is None
        assert await session.get(AuditRecord, audit_id) is None


@pytest.mark.asyncio
async def test_future_replay_rejected_without_live_fallback(database: Database) -> None:
    task_id = await create_task(database)
    fake = FakeTool()
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        entry = ToolDispatcher(registry_for(fake), PolicyEngine(PolicyEnvironment.TEST), ledger)
        live = await entry.dispatch(
            task_id=task_id,
            tool_name="get_service_context",
            parameters={"service": "payment-service"},
            actor="AI",
        )
        assert live.evidence_id is not None
        evidence = await ledger.get_evidence(live.evidence_id)
        replay = await entry.dispatch(
            task_id=task_id,
            tool_name="get_service_context",
            parameters={"service": "payment-service"},
            actor="AI",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=live.evidence_id,
            replay_before=evidence.collected_at - timedelta(seconds=1),
        )
        assert replay.error_code == "replay_mismatch" and len(fake.calls) == 1
        assert len(await ledger.evidence_for_task(task_id)) == 1


@pytest.mark.asyncio
async def test_nonexistent_task_rejected_before_execution(database: Database) -> None:
    fake = FakeTool()
    async with database.session() as session, session.begin():
        with pytest.raises(TaskNotFound):
            await ToolDispatcher(
                registry_for(fake), PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
            ).dispatch(task_id=uuid4(), tool_name="get_service_context", parameters={}, actor="AI")
        assert fake.calls == []


@pytest.mark.parametrize("tool_name", ["get_k8s_status", "get_service_runtime", "query_events"])
@pytest.mark.asyncio
async def test_kubernetes_evidence_commit_and_replay(database: Database, tool_name: str) -> None:
    """Step 12：三个真实 Tool 使用 Fake 源，在跨会话中精确引用证据。"""
    task_id = await create_task(database)
    parameters: JsonObject = {"namespace": "payment", "service_name": "payment-service"}
    async with FakeKubernetesConnector() as connector:
        registry = ToolRegistry()
        register_kubernetes_tools(registry, connector)
        async with database.session() as session, session.begin():
            live = await ToolDispatcher(
                registry, PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
            ).dispatch(
                task_id=task_id, tool_name=tool_name, parameters=parameters, actor="fake-agent"
            )
        assert live.status is DispatchStatus.SUCCEEDED and live.evidence_id is not None
        await connector.aclose()
        async with database.session() as session, session.begin():
            ledger = LedgerService(session)
            evidence = await ledger.evidence_for_task(task_id)
            audits = [
                audit
                for audit in await ledger.audits_for_task(task_id)
                if audit.event_type is AuditEventType.TOOL_CALL
            ]
            assert len(evidence) == len(audits) == 1
            assert evidence[0].id == live.evidence_id == audits[0].evidence_id
            assert evidence[0].source_tool == tool_name and evidence[0].parameters == parameters
            assert evidence[0].result_snapshot == live.result
            assert evidence[0].collected_at.tzinfo is UTC
            replay = await ToolDispatcher(
                registry, PolicyEngine(PolicyEnvironment.TEST), ledger
            ).dispatch(
                task_id=task_id,
                tool_name=tool_name,
                parameters=parameters,
                actor="replay-agent",
                mode=DispatchMode.REPLAY,
                replay_evidence_id=live.evidence_id,
                replay_before=utc_now(),
            )
            assert replay.status is DispatchStatus.REPLAYED and replay.result == live.result
        async with database.session() as session:
            ledger = LedgerService(session)
            assert len(await ledger.evidence_for_task(task_id)) == 1
            audits = [
                audit
                for audit in await ledger.audits_for_task(task_id)
                if audit.event_type is AuditEventType.TOOL_CALL
            ]
            assert [audit.outcome for audit in audits] == ["succeeded", "replayed"]


@pytest.mark.parametrize("tool_name", ["compare_versions", "get_recent_deployments"])
@pytest.mark.asyncio
async def test_changes_evidence_commit_and_replay(database: Database, tool_name: str) -> None:
    """Step 14：跨会话验证两个 Tool 的真实 Ledger 证据、审计及关闭源后的回放。"""
    task_id = await create_task(database)
    parameters: JsonObject = (
        {"service_name": "payment-service", "from_version": "v2.3.6", "to_version": "v2.3.7"}
        if tool_name == "compare_versions"
        else {
            "service_name": "payment-service",
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-10-01T02:00:00Z",
        }
    )
    async with (
        FakeGitConnector() as git,
        FakeCIConnector() as ci,
        FakeArgoCDConnector() as argo,
        FakeConfigCenterConnector() as config,
    ):
        registry = ToolRegistry()
        register_change_tools(registry, git, ci, argo, config)
        async with database.session() as session, session.begin():
            live = await ToolDispatcher(
                registry, PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
            ).dispatch(
                task_id=task_id, tool_name=tool_name, parameters=parameters, actor="fake-agent"
            )
        assert live.status is DispatchStatus.SUCCEEDED and live.evidence_id is not None
        for connector in (git, ci, argo, config):
            await connector.aclose()
        async with database.session() as session, session.begin():
            ledger = LedgerService(session)
            evidence = await ledger.evidence_for_task(task_id)
            audits = [
                a
                for a in await ledger.audits_for_task(task_id)
                if a.event_type is AuditEventType.TOOL_CALL
            ]
            assert len(evidence) == len(audits) == 1
            assert evidence[0].id == live.evidence_id == audits[0].evidence_id
            assert evidence[0].parameters == parameters and evidence[0].source_tool == tool_name
            assert evidence[0].result_snapshot == live.result
            assert evidence[0].collected_at.tzinfo is UTC
            replay = await ToolDispatcher(
                registry, PolicyEngine(PolicyEnvironment.TEST), ledger
            ).dispatch(
                task_id=task_id,
                tool_name=tool_name,
                parameters=parameters,
                actor="replay-agent",
                mode=DispatchMode.REPLAY,
                replay_evidence_id=live.evidence_id,
                replay_before=utc_now(),
            )
            assert replay.status is DispatchStatus.REPLAYED and replay.result == live.result
            assert replay.evidence_id == live.evidence_id
        async with database.session() as session:
            ledger = LedgerService(session)
            assert len(await ledger.evidence_for_task(task_id)) == 1
            audits = [
                a
                for a in await ledger.audits_for_task(task_id)
                if a.event_type is AuditEventType.TOOL_CALL
            ]
            assert [audit.outcome for audit in audits] == ["succeeded", "replayed"]


@pytest.mark.parametrize("tool_name", ["query_metrics", "query_logs", "query_traces"])
@pytest.mark.asyncio
async def test_observability_evidence_commit_and_replay(database: Database, tool_name: str) -> None:
    """Step 13：三个可观测性 Tool 跨会话持久化、证据精确引用及离线回放。"""
    task_id = await create_task(database)
    parameters: JsonObject = {
        "service_name": "payment-service",
        "start": "2026-10-01T01:00:00Z",
        "end": "2026-10-01T01:10:00Z",
    }
    async with (
        FakePrometheusConnector() as prom,
        FakeSLSConnector() as sls,
        FakeARMSConnector() as arms,
    ):
        registry = ToolRegistry()
        register_observability_tools(registry, prom, sls, arms)
        async with database.session() as session, session.begin():
            live = await ToolDispatcher(
                registry, PolicyEngine(PolicyEnvironment.TEST), LedgerService(session)
            ).dispatch(
                task_id=task_id, tool_name=tool_name, parameters=parameters, actor="fake-agent"
            )
        assert live.status is DispatchStatus.SUCCEEDED and live.evidence_id is not None
        for client in (prom, sls, arms):
            await client.aclose()
        async with database.session() as session, session.begin():
            ledger = LedgerService(session)
            evidence = await ledger.evidence_for_task(task_id)
            audits = [
                a
                for a in await ledger.audits_for_task(task_id)
                if a.event_type is AuditEventType.TOOL_CALL
            ]
            assert len(evidence) == len(audits) == 1
            assert evidence[0].id == live.evidence_id == audits[0].evidence_id
            assert (
                evidence[0].source_tool == tool_name and evidence[0].result_snapshot == live.result
            )
            assert evidence[0].parameters["start"] == parameters["start"]
            assert evidence[0].collected_at.tzinfo is UTC
            replay = await ToolDispatcher(
                registry, PolicyEngine(PolicyEnvironment.TEST), ledger
            ).dispatch(
                task_id=task_id,
                tool_name=tool_name,
                parameters=parameters,
                actor="replay-agent",
                mode=DispatchMode.REPLAY,
                replay_evidence_id=live.evidence_id,
                replay_before=utc_now(),
            )
            assert replay.status is DispatchStatus.REPLAYED and replay.result == live.result
            assert replay.evidence_id == live.evidence_id
        async with database.session() as session:
            ledger = LedgerService(session)
            assert len(await ledger.evidence_for_task(task_id)) == 1
            audits = [
                a
                for a in await ledger.audits_for_task(task_id)
                if a.event_type is AuditEventType.TOOL_CALL
            ]
            assert [audit.outcome for audit in audits] == ["succeeded", "replayed"]
