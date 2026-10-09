"""隔离 PostgreSQL/Temporal：真实向量、证据、条件分支与重试回放。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities
from app.agent.investigation import AgentConclusion
from app.agent.reviewer.activities import ReviewerActivities
from app.agent.workflow_models import InvestigationRequest
from app.config import Settings, parse_database_url
from app.db.base import utc_now
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.activities import RunbookActivities
from app.runbooks.embedding import embedding_client
from app.runbooks.models import Runbook
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import RunbookDraft, RunbookSearch, RunbookView
from app.runbooks.service import RunbookNotFound, RunbookService
from app.runbooks.workflow_models import RunbookMatchRequest, RunbookMatchResult
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.safety.activities import SafetyActivities
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus
from app.tasks.worker import start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest, WorkflowInput
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.registry import ToolRegistry
from app.tools.runbooks import register_runbook_tools
from tests.database_support import get_test_database_url, migrate
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import accept, agent_worker, new_task, runtime_settings, seed
from tests.test_workflow_integration import wait_at

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="请执行 check-runbooks.ps1 使用独立本机依赖"
)
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal 专项入口"
)


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    database = Database(parse_database_url(get_test_database_url()))
    try:
        async with database.session() as session, session.begin():
            await session.execute(delete(Runbook))
        await seed(database)
        yield database
    finally:
        await database.dispose()


def service(session: object) -> RunbookService:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    settings = Settings(APP_ENV="test")
    return RunbookService(session, lambda request: embedding_client(settings, request))


async def save(database: Database, **updates: object) -> RunbookView:
    data = payment_runbook().model_dump(mode="json")
    data.update(updates)
    draft = RunbookDraft.model_validate_json(json.dumps(data))
    async with database.session() as session, session.begin():
        return await service(session).create(draft)


async def matching(database: Database) -> TaskSnapshot:
    task = await new_task(database)
    store = TaskActivityStore(database)
    for status in (TaskStatus.CONTEXT_BUILDING, TaskStatus.RUNBOOK_MATCHING):
        task = await store.transition(TransitionRequest(task, status, "Step 25 准备匹配"))
    return task


@pytest.mark.asyncio
async def test_crud_semantic_ranking_update_vector_and_delete(database: Database) -> None:
    payment = await save(database)
    await save(
        database,
        name="network-dns",
        description="network DNS 故障",
        applicability_conditions=[
            {"field": "service_name", "operator": "equals", "value": "network-service"}
        ],
    )
    async with database.session() as session:
        result = await service(session).search(RunbookSearch(query="payment-service 5xx"))
        assert result[0].runbook.id == payment.id
        assert result[0].similarity > 0.99
        assert result[0].runbook.created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    changed = payment_runbook().model_copy(update={"description": "network DNS 容量调查"})
    async with database.session() as session, session.begin():
        before = await session.get(Runbook, payment.id)
        assert before is not None
        vector = list(before.embedding)
        updated = await service(session).update(payment.id, changed)
        assert updated.description == changed.description
        await session.refresh(before)
        assert list(before.embedding) != vector
    async with database.session() as session, session.begin():
        await service(session).delete(payment.id)
    async with database.session() as session:
        assert payment.id not in {
            hit.runbook.id
            for hit in await service(session).search(RunbookSearch(query="payment 5xx"))
        }
        with pytest.raises(RunbookNotFound):
            await service(session).get(payment.id)


@pytest.mark.asyncio
async def test_missing_required_fields_fail_before_embedding_and_write(database: Database) -> None:
    from app.agent.client import LLMClient
    from app.agent.models import EmbeddingRequest

    def no_embedding(request: EmbeddingRequest) -> LLMClient:
        raise AssertionError("无效 Runbook 不得生成向量")

    from pydantic import ValidationError

    async with database.session() as session, session.begin():
        for field in RunbookDraft.model_fields:
            data = payment_runbook().model_dump()
            del data[field]
            invalid = RunbookDraft.model_construct(**data)
            with pytest.raises(ValidationError):
                await RunbookService(session, no_embedding).create(invalid)
        assert (await session.scalars(select(Runbook))).all() == []


@pytest.mark.asyncio
async def test_database_not_null_and_counter_vector_constraints(database: Database) -> None:
    for field in RunbookDraft.model_fields:
        async with database.session() as session:
            values = await service(session)._values(payment_runbook())
            values[field] = None
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(Runbook(**values))
                    await session.flush()
    for field, value in (("confidence", 2), ("success_count", -1), ("embedding", [0.0] * 4)):
        async with database.session() as session:
            values = await service(session)._values(payment_runbook())
            values[field] = value
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(Runbook(**values))
                    await session.flush()


@pytest.mark.asyncio
async def test_tool_live_and_replay_use_one_evidence_and_never_requery(database: Database) -> None:
    await save(database)
    task = await new_task(database)
    async with database.session() as session, session.begin():
        registry = ToolRegistry()
        register_runbook_tools(registry, service(session))
        ledger = LedgerService(session)
        dispatcher = ToolDispatcher(
            registry, create_policy_engine(Settings(APP_ENV="test")), ledger
        )
        result = await dispatcher.dispatch(
            task_id=UUID(task.task_id),
            tool_name="search_runbooks",
            parameters={"query": "payment 5xx"},
            actor="验收",
        )
        assert result.status is DispatchStatus.SUCCEEDED and result.evidence_id
        assert len(await ledger.evidence_for_task(UUID(task.task_id))) == 1
        assert (
            len(
                [
                    a
                    for a in await ledger.audits_for_task(UUID(task.task_id))
                    if a.event_type is AuditEventType.TOOL_CALL
                ]
            )
            == 1
        )
    async with database.session() as session, session.begin():

        def forbidden(request: object) -> object:
            raise AssertionError("Replay 不得生成新向量或查询历史以外内容")

        from app.agent.client import LLMClient
        from app.agent.models import EmbeddingRequest

        def no_llm(request: EmbeddingRequest) -> LLMClient:
            forbidden(request)
            raise AssertionError

        registry = ToolRegistry()
        register_runbook_tools(registry, RunbookService(session, no_llm))
        replay = await ToolDispatcher(
            registry, create_policy_engine(Settings(APP_ENV="test")), LedgerService(session)
        ).dispatch(
            task_id=UUID(task.task_id),
            tool_name="search_runbooks",
            parameters={"query": "payment 5xx"},
            actor="验收回放",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=result.evidence_id,
            replay_before=utc_now(),
        )
        assert replay.status is DispatchStatus.REPLAYED
        assert replay.result == result.result and replay.evidence_id == result.evidence_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["applicable", "excluded", "inapplicable", "draft", "empty"])
async def test_matching_branches_and_frozen_concurrent_retry(database: Database, mode: str) -> None:
    if mode != "empty":
        await save(database, maturity="draft" if mode == "draft" else "verified")
    spec = SPEC.model_copy(update={"title": "payment 维护 5xx"}) if mode == "excluded" else SPEC
    if mode == "inapplicable":
        spec = SPEC.model_copy(update={"title": "支付正常"})
    task = await matching(database)
    request = RunbookMatchRequest(task, spec.model_dump_json())
    activities = RunbookActivities(database, Settings(APP_ENV="test"))
    first, concurrent = await asyncio.gather(activities.match(request), activities.match(request))
    assert first == concurrent and not first.blocked
    assert bool(first.runbook_json) == (mode == "applicable")
    if mode == "excluded":
        assert "排除条件" in first.reason
    async with database.session() as session, session.begin():
        await session.execute(delete(Runbook))
    assert await activities.match(request) == first
    with pytest.raises(ApplicationError, match="被拒绝"):
        await activities.match(
            replace(
                request, spec_json=SPEC.model_copy(update={"title": "其他请求"}).model_dump_json()
            )
        )
    async with database.session() as session:
        evidence = await LedgerService(session).evidence_for_task(UUID(task.task_id))
        assert [e.source_tool for e in evidence] == ["search_runbooks", "runbook.match"]


@pytest.mark.asyncio
async def test_policy_rejection_commits_audit_and_blocks_matching(database: Database) -> None:
    task = await matching(database)
    settings = Settings(
        APP_ENV="test",
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "block-runbooks",
                    "action_names": ["search_runbooks"],
                    "risk_levels": ["L0"],
                    "environments": ["test"],
                    "decision": "deny",
                    "reason": "验收拒绝",
                }
            ]
        },
    )
    result = await RunbookActivities(database, settings).match(
        RunbookMatchRequest(task, SPEC.model_dump_json())
    )
    assert result.blocked and result.search_evidence_id is None
    async with database.session() as session:
        ledger = LedgerService(session)
        assert any(
            a.operation == "search_runbooks" and a.outcome == "rejected"
            for a in await ledger.audits_for_task(UUID(task.task_id))
        )
        assert all(
            e.source_tool != "search_runbooks"
            for e in await ledger.evidence_for_task(UUID(task.task_id))
        )


@pytest.mark.asyncio
async def test_guide_uses_saved_match_and_real_evidence_and_reuses_observations(
    database: Database,
) -> None:
    await save(database)
    task = await matching(database)
    match = await RunbookActivities(database, Settings(APP_ENV="test")).match(
        RunbookMatchRequest(task, SPEC.model_dump_json())
    )
    assert match.runbook_json
    task = await TaskActivityStore(database).transition(
        TransitionRequest(task, TaskStatus.INVESTIGATING, match.reason)
    )
    activities = AgentActivities(database, Settings(APP_ENV="test"))
    request = InvestigationRequest(task, SPEC.model_dump_json(), match.runbook_json)
    result = await activities.investigate(request)
    assert result.steps == 5
    assert await activities.investigate(request) == result
    with pytest.raises(ApplicationError, match="被拒绝"):
        await activities.investigate(replace(request, runbook_json=None))
    with pytest.raises(ApplicationError, match="被拒绝"):
        await activities.investigate(
            replace(request, runbook_json=match.runbook_json.replace("0.8", "0.9"))
        )
    await accept(database, task, result)
    async with database.session() as session:
        ledger = LedgerService(session)
        evidence = await ledger.evidence_for_task(UUID(task.task_id))
        observations = [e for e in evidence if e.source_tool == "agent.observe"]
        assert [e.parameters["step"] for e in observations] == [1, 2, 3, 4]
        conclusion = AgentConclusion.model_validate_json(result.conclusion_json)
        assert len(conclusion.evidence_ids) == 4
        assert {UUID(value) for value in result.observed_ids} == conclusion.evidence_ids
        assert "execute_action" not in [
            a.operation for a in await ledger.audits_for_task(UUID(task.task_id))
        ]


@pytest.mark.asyncio
async def test_matching_evidence_failure_rolls_back_search_and_audit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await matching(database)
    original = LedgerService.append_evidence

    async def fail(self: LedgerService, **arguments: object) -> Evidence:
        if arguments["source_tool"] == "runbook.match":
            raise RuntimeError("匹配结果写入失败")
        return await original(self, **arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(ApplicationError):
        await RunbookActivities(database, Settings(APP_ENV="test")).match(
            RunbookMatchRequest(task, SPEC.model_dump_json())
        )
    async with database.session() as session:
        ledger = LedgerService(session)
        assert await ledger.evidence_for_task(UUID(task.task_id)) == []
        assert all(
            a.event_type is not AuditEventType.TOOL_CALL
            for a in await ledger.audits_for_task(UUID(task.task_id))
        )


@local_temporal
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["applicable", "excluded", "empty"])
async def test_temporal_matching_investigating_rca_and_replay(
    database: Database, mode: str
) -> None:
    if mode != "empty":
        await save(database)
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    spec = SPEC.model_copy(update={"title": "payment 维护 5xx"}) if mode == "excluded" else SPEC
    handle = None
    try:
        async with agent_worker(client, database, settings):
            handle = await start_task_workflow(
                client,
                WorkflowInput(task.task_id, investigation_json=spec.model_dump_json()),
                task_queue=settings.temporal_config.task_queue,
            )
            await wait_at(handle, TaskStatus.WAITING_APPROVAL)
            progress = await handle.query(AITaskWorkflow.progress)
            assert progress.conclusion_json and progress.conclusion_evidence_id
            async with database.session() as session:
                ledger = LedgerService(session)
                audits = [
                    a
                    for a in await ledger.audits_for_task(UUID(task.task_id))
                    if a.event_type is AuditEventType.TOOL_CALL
                ]
                assert [a.operation for a in audits] == [
                    "search_runbooks",
                    "get_service_context",
                    "get_recent_changes",
                    "query_metrics",
                    "query_logs",
                    "query_traces",
                    "get_recent_changes",
                ]
                evidence = await ledger.evidence_for_task(UUID(task.task_id))
                conclusion = next(e for e in evidence if e.source_tool == "agent.conclusion")
                assert conclusion.parameters["steps"] == (5 if mode == "applicable" else 9)
                history = await TaskService(session).history(UUID(task.task_id))
                if mode == "excluded":
                    assert "排除条件" in next(
                        h.reason for h in history if h.to_status is TaskStatus.INVESTIGATING
                    )
                assert TaskStatus.EXECUTING not in [h.to_status for h in history]
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 25 验收清理")


@local_temporal
@pytest.mark.asyncio
async def test_temporal_matching_commit_response_loss_restart_reuses_snapshot(
    database: Database,
) -> None:
    await save(database)
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    matcher = RunbookActivities(database, settings)
    agent = AgentActivities(database, settings)
    tasks = TaskActivities(TaskActivityStore(database))
    calls = 0
    committed = asyncio.Event()

    @activity.defn(name="runbook.match")
    async def lost_response(request: RunbookMatchRequest) -> RunbookMatchResult:
        nonlocal calls
        result = await matcher.match(request)
        calls += 1
        if calls == 1:
            committed.set()
            raise ApplicationError("提交后丢失匹配响应")
        return result

    def worker() -> Worker:
        return Worker(
            client,
            task_queue=settings.temporal_config.task_queue,
            workflows=[AITaskWorkflow],
            activities=[
                SafetyActivities(database, settings).check,
                SafetyActivities(database, settings).notify,
                tasks.load,
                tasks.transition,
                tasks.placeholder_stage,
                lost_response,
                agent.investigate,
                agent.validate_conclusion,
                ReviewerActivities(database, settings).review,
            ],
            max_cached_workflows=0,
        )

    async with worker():
        handle = await start_task_workflow(
            client,
            WorkflowInput(
                task.task_id, investigation_json=SPEC.model_dump_json(), human_timeout_seconds=0.1
            ),
            task_queue=settings.temporal_config.task_queue,
        )
        await asyncio.wait_for(committed.wait(), 30)
    async with database.session() as session, session.begin():
        await session.execute(delete(Runbook))
    async with worker():
        await asyncio.wait_for(handle.result(), 30)
    assert calls == 2
    async with database.session() as session:
        evidence = await LedgerService(session).evidence_for_task(UUID(task.task_id))
        assert len([e for e in evidence if e.source_tool == "search_runbooks"]) == 1
        assert (
            next(e for e in evidence if e.source_tool == "agent.conclusion").parameters["steps"]
            == 5
        )
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
