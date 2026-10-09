"""Step 41 本机 PostgreSQL/Temporal：真实留证、门禁、并发和历史回放。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, EmbeddingRequest
from app.config import Settings, parse_database_url
from app.db.base import utc_now
from app.db.session import Database
from app.knowledge.schemas import KnowledgeDraft, KnowledgeType
from app.knowledge.service import KnowledgeService
from app.learning.evaluation.demo import closed_fake_incident
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.embedding import embedding_client
from app.tasks.activities import TaskActivityStore
from app.tasks.architecture.activities import ArchitectureActivities
from app.tasks.architecture.demo import seed_sample
from app.tasks.architecture.models import (
    ArchitectureReport,
    ArchitectureRequest,
    ArchitectureResult,
    ArchitectureVerifyRequest,
    ReviewSubmission,
)
from app.tasks.architecture.scenario import SAMPLE_PROPOSAL, SAMPLE_STANDARD, sample_response
from app.tasks.architecture.service import submit_review
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor, VerificationRequired
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest
from app.tools.architecture import architecture_registry
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus
from app.triggers.activities import EventActivities
from app.triggers.schemas import EventReceipt
from app.verifier.architecture import ArchitectureVerifier
from tests.database_support import get_test_database_url, migrate

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="运行 check-architecture.ps1 使用隔离本机依赖",
    ),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        await seed_sample(instance)
        yield instance
    finally:
        await instance.dispose()


def value() -> ReviewSubmission:
    return ReviewSubmission(
        request_id=uuid4(),
        service_name="payment-service",
        title="支付技术方案",
        proposal=SAMPLE_PROPOSAL,
    )


async def submit(database: Database, proposal: ReviewSubmission | None = None) -> EventReceipt:
    async with database.session() as session, session.begin():
        return await submit_review(session, proposal or value())


async def prepare(database: Database) -> ArchitectureRequest:
    receipt = await submit(database)
    task = TaskSnapshot(receipt.task_id, TaskStatus.NEW, 0)
    for phase in (
        TaskStatus.CONTEXT_BUILDING,
        TaskStatus.RUNBOOK_MATCHING,
        TaskStatus.INVESTIGATING,
    ):
        task = await TaskActivityStore(database).transition(
            TransitionRequest(task, phase, "架构评审测试")
        )
    return ArchitectureRequest(task)


async def verifying(database: Database, request: ArchitectureRequest) -> TaskSnapshot:
    task = request.task
    for phase in (TaskStatus.RCA, TaskStatus.PLANNING, TaskStatus.EXECUTING, TaskStatus.VERIFYING):
        task = await TaskActivityStore(database).transition(
            TransitionRequest(task, phase, "交付只读评审")
        )
    return task


async def test_twelve_dimensions_real_sources_citations_and_no_actions(database: Database) -> None:
    request = await prepare(database)
    result = await ArchitectureActivities(database, Settings(APP_ENV="test")).review(request)
    report = ArchitectureReport.model_validate_json(result.report_json or "{}")
    assert len(report.dimensions) == 12 and all(d.outcome == "risk" for d in report.dimensions[:2])
    assert all(
        any(c.evidence_id == report.sources.standards for c in d.citations)
        for d in report.dimensions[:2]
    )
    async with database.session() as session:
        ledger = LedgerService(session)
        for source in report.sources.ids:
            assert (await ledger.get_evidence(source)).task_id == UUID(request.task.task_id)
        audits = await ledger.audits_for_task(UUID(request.task.task_id))
        calls = [a for a in audits if a.event_type is AuditEventType.TOOL_CALL]
        assert [a.operation for a in calls] == [
            "search_runbooks",
            "get_service_context",
            "search_knowledge",
            "search_incidents",
        ]
        assert all(a.outcome == "succeeded" for a in calls)
        assert not any(a.event_type is AuditEventType.EXECUTION for a in audits)


async def test_duplicate_submission_and_changed_proposal_rejected(database: Database) -> None:
    proposal = value()
    first, second = await asyncio.gather(submit(database, proposal), submit(database, proposal))
    assert first.task_id == second.task_id and first.duplicate != second.duplicate
    with pytest.raises(ValueError):
        await submit(database, proposal.model_copy(update={"proposal": "不同方案"}))
    with pytest.raises(ValueError):
        await submit(database, proposal.model_copy(update={"service_name": "other-service"}))


async def test_parallel_and_lost_response_reuse_report_without_more_llm(database: Database) -> None:
    request = await prepare(database)
    llms: list[FakeLLM] = []

    def factory() -> FakeLLM:
        llm = FakeLLM([ScriptedChatStep(sample_response)])
        llms.append(llm)
        return llm

    activities = ArchitectureActivities(database, Settings(APP_ENV="test"), llm_factory=factory)
    first, second = await asyncio.gather(activities.review(request), activities.review(request))
    assert first == second == await activities.review(request)
    assert sum(len(llm.calls) for llm in llms) == 1
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(request.task.task_id))
        assert sum(a.event_type is AuditEventType.TOOL_CALL for a in audits) == 4


async def test_policy_denial_commits_audit_and_prevents_model_generation(
    database: Database,
) -> None:
    request = await prepare(database)
    settings = Settings(
        APP_ENV="test",
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "deny-architecture-knowledge",
                    "action_names": ["search_knowledge"],
                    "risk_levels": ["L0"],
                    "reason": "专项拒绝验证",
                    "decision": "deny",
                }
            ]
        },
    )
    llm = FakeLLM()
    result = await ArchitectureActivities(database, settings, llm_factory=lambda: llm).review(
        request
    )
    assert result.blocked and result.report_json is None and not llm.calls
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(request.task.task_id))
        assert any(a.operation == "search_knowledge" and a.outcome == "rejected" for a in audits)


async def test_expired_company_standard_not_returned(database: Database) -> None:
    settings = Settings(APP_ENV="test")
    llm = embedding_client(settings, EmbeddingRequest(inputs=(SAMPLE_STANDARD,)))
    try:
        async with database.session() as session, session.begin():
            expired = await KnowledgeService(session, llm).create(
                KnowledgeDraft(
                    kind=KnowledgeType.STANDARD,
                    content=SAMPLE_STANDARD,
                    source="过期规范",
                    valid_from=utc_now() - timedelta(days=2),
                    expires_at=utc_now() - timedelta(days=1),
                )
            )
    finally:
        await llm.aclose()
    request = await prepare(database)
    result = await ArchitectureActivities(database, settings).review(request)
    report = ArchitectureReport.model_validate_json(result.report_json or "{}")
    async with database.session() as session:
        evidence = await LedgerService(session).get_evidence(report.sources.standards)
        matches = json.loads(json.dumps(evidence.result_snapshot))["matches"]
        assert matches and all(m["entry"]["id"] != str(expired.id) for m in matches)


@pytest.mark.parametrize("forgery", ["invented_id", "wrong_quote", "cross_task"])
async def test_fake_model_bad_references_do_not_save_report(
    database: Database, forgery: str
) -> None:
    foreign = await prepare(database)
    other = await ArchitectureActivities(database, Settings(APP_ENV="test")).review(foreign)
    other_report = ArchitectureReport.model_validate_json(other.report_json or "{}")

    def response(request: ChatRequest) -> ChatResponse:
        original = sample_response(request)
        content = json.loads(original.message.content or "{}")
        citation = content["dimensions"][0]["citations"][0]
        if forgery == "wrong_quote":
            citation["quote"] = "来源中从未出现的恢复演练"
        else:
            citation["evidence_id"] = str(
                other_report.sources.proposal if forgery == "cross_task" else uuid4()
            )
        return original.model_copy(
            update={"message": ChatMessage(role="assistant", content=json.dumps(content))}
        )

    request = await prepare(database)
    with pytest.raises(ApplicationError):
        await ArchitectureActivities(
            database,
            Settings(APP_ENV="test"),
            llm_factory=lambda: FakeLLM([ScriptedChatStep(response)]),
        ).review(request)
    async with database.session() as session:
        records = await LedgerService(session).evidence_for_task(UUID(request.task.task_id))
        assert not any(e.source_tool == "architecture.report" for e in records)
        assert sum(e.source_tool == "architecture.context" for e in records) == 1
        audits = await LedgerService(session).audits_for_task(UUID(request.task.task_id))
        assert sum(a.event_type is AuditEventType.TOOL_CALL for a in audits) == 4
    await ArchitectureActivities(database, Settings(APP_ENV="test")).review(request)
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(request.task.task_id))
        assert sum(a.event_type is AuditEventType.TOOL_CALL for a in audits) == 4


async def test_independent_verifier_resolves_and_retry_is_idempotent(database: Database) -> None:
    request = await prepare(database)
    settings = Settings(APP_ENV="test")
    result = await ArchitectureActivities(database, settings).review(request)
    task = await verifying(database, request)
    async with database.session() as session, session.begin():
        with pytest.raises(VerificationRequired):
            await TaskService(session).transition(
                UUID(task.task_id),
                TaskStatus.RESOLVED,
                expected_status=task.status,
                expected_version=task.version,
                reason="伪造 verifier 身份",
                actor=TransitionActor.VERIFIER,
            )
    verifier = ArchitectureVerifier(database, settings)
    verify_request = ArchitectureVerifyRequest(task, result.evidence_id)
    first, second = await asyncio.gather(
        verifier.verify(verify_request), verifier.verify(verify_request)
    )
    assert first == second and first.status is TaskStatus.RESOLVED


@pytest.mark.parametrize("change", ["version", "report", "missing_audit"])
async def test_verifier_rejects_wrong_version_cross_report_and_forged_sources(
    database: Database, change: str
) -> None:
    request = await prepare(database)
    settings = Settings(APP_ENV="test")
    result = await ArchitectureActivities(database, settings).review(request)
    task = await verifying(database, request)
    if change == "version":
        task = replace(task, version=task.version + 1)
    elif change == "report":
        result = await ArchitectureActivities(database, settings).review(await prepare(database))
    else:
        async with database.session() as session, session.begin():
            ledger = LedgerService(session)
            original = await ledger.get_evidence(UUID(result.evidence_id))
            data = json.loads(json.dumps(original.result_snapshot))
            fact = await ledger.get_evidence(UUID(data["sources"]["context"]))
            forged = await ledger.append_evidence(
                task_id=UUID(task.task_id),
                source_tool=fact.source_tool,
                parameters=fact.parameters,
                result_snapshot=fact.result_snapshot,
            )
            data["sources"]["context"] = str(forged.id)
            report = await ledger.append_evidence(
                task_id=UUID(task.task_id),
                source_tool=original.source_tool,
                parameters=original.parameters,
                result_snapshot=data,
            )
            result = replace(result, evidence_id=str(report.id))
    with pytest.raises(ApplicationError):
        await ArchitectureVerifier(database, settings).verify(
            ArchitectureVerifyRequest(task, result.evidence_id)
        )
    async with database.session() as session:
        stored = await session.get(AITask, UUID(task.task_id))
        assert stored and stored.status is TaskStatus.VERIFYING


async def test_knowledge_dispatcher_replay_without_embedding_calls(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await prepare(database)
    settings = Settings(APP_ENV="test")
    result = await ArchitectureActivities(database, settings).review(request)
    report = ArchitectureReport.model_validate_json(result.report_json or "{}")

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Replay 不应调用 embedding 或源系统")

    monkeypatch.setattr("app.tools.knowledge.embedding_client", blocked)
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        evidence = await ledger.get_evidence(report.sources.standards)
        replay = await ToolDispatcher(
            architecture_registry(session, settings), create_policy_engine(settings), ledger
        ).dispatch(
            task_id=UUID(request.task.task_id),
            tool_name="search_knowledge",
            parameters=evidence.parameters,
            actor="replay",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=evidence.id,
            replay_before=utc_now(),
        )
        assert (
            replay.status is DispatchStatus.REPLAYED and replay.result == evidence.result_snapshot
        )


@local_temporal
async def test_context_and_real_historical_incident_reach_model_and_report(
    database: Database,
) -> None:
    incident = await closed_fake_incident(
        database, Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})
    )

    def response(request: ChatRequest) -> ChatResponse:
        payload = json.loads(request.messages[-1].content or "{}")
        sources = payload["sources"]
        context = payload["snapshots"][sources["context"]]
        assert any(n["external_id"] == "payment-db" for n in context["nodes"])
        assert all(
            "source" in e and "confidence" in e and "freshness_seconds" in e
            for e in context["edges"]
        )
        histories = payload["snapshots"][sources["incidents"]]["matches"]
        hit = next(h for h in histories if h["report"]["task_id"] == str(incident.task_id))
        quote = hit["report"]["sections"][4]["conclusions"][0]["statement"]
        original = sample_response(request)
        data = json.loads(original.message.content or "{}")
        data["dimensions"][2]["finding"] = (
            "历史事故提示需要复核数据库容量，当前新方案容量材料仍不足。"
        )
        data["dimensions"][2]["citations"].append(
            {"evidence_id": sources["incidents"], "quote": quote}
        )
        return original.model_copy(
            update={"message": ChatMessage(role="assistant", content=json.dumps(data))}
        )

    request = await prepare(database)
    result = await ArchitectureActivities(
        database,
        Settings(APP_ENV="test"),
        llm_factory=lambda: FakeLLM([ScriptedChatStep(response)]),
    ).review(request)
    report = ArchitectureReport.model_validate_json(result.report_json or "{}")
    assert report.dimensions[2].outcome == "unknown"
    assert any(c.evidence_id == report.sources.incidents for c in report.dimensions[2].citations)
    task = await verifying(database, request)
    assert (
        await ArchitectureVerifier(database, Settings(APP_ENV="test")).verify(
            ArchitectureVerifyRequest(task, result.evidence_id)
        )
    ).status is TaskStatus.RESOLVED


@local_temporal
async def test_event_dispatch_complete_workflow_history_and_replay(database: Database) -> None:
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"architecture-test-{uuid4().hex}",
        },
    )
    receipt = await submit(database)
    async with create_worker(client, database, settings, max_cached_workflows=0):
        events = EventActivities(database, settings, client)
        await events.start_task(receipt)
        await events.start_task(receipt)
        handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
        try:
            result = await asyncio.wait_for(handle.result(), timeout=60)
        except BaseException:
            await handle.terminate("清理本机架构评审专项任务")
            raise
    assert result.task and result.task.status is TaskStatus.CLOSED
    async with database.session() as session:
        history = await TaskService(session).history(UUID(receipt.task_id))
        assert [h.to_status for h in history] == [h.status for h in result.history]
        assert [h.sequence for h in history] == [h.version for h in result.history]
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


@local_temporal
async def test_worker_restart_after_committed_report_does_not_repeat_queries(
    database: Database,
) -> None:
    committed = asyncio.Event()
    llms: list[FakeLLM] = []

    def factory() -> FakeLLM:
        llm = FakeLLM([ScriptedChatStep(sample_response)])
        llms.append(llm)
        return llm

    class LostReplyActivities(ArchitectureActivities):
        attempts = 0

        @activity.defn(name="architecture.review")
        async def review(self, request: ArchitectureRequest) -> ArchitectureResult:
            result = await super().review(request)
            self.attempts += 1
            if self.attempts == 1:
                committed.set()
                raise ApplicationError("模拟报告提交后丢失响应")
            return result

    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"architecture-restart-{uuid4().hex}",
        },
    )
    activities = LostReplyActivities(database, settings, llm_factory=factory)
    receipt = await submit(database)
    handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
    try:
        async with create_worker(client, database, settings, architecture_activities=activities):
            await EventActivities(database, settings, client).start_task(receipt)
            await asyncio.wait_for(committed.wait(), timeout=30)
        async with create_worker(client, database, settings, architecture_activities=activities):
            progress = await asyncio.wait_for(handle.result(), timeout=60)
    except BaseException:
        await handle.terminate("清理架构评审 Worker 重启测试")
        raise
    assert progress.task and progress.task.status is TaskStatus.CLOSED
    assert sum(len(llm.calls) for llm in llms) == 1 and activities.attempts >= 2
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(receipt.task_id))
        assert (
            sum(
                a.actor == "architecture-review" and a.event_type is AuditEventType.TOOL_CALL
                for a in audits
            )
            == 4
        )
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
