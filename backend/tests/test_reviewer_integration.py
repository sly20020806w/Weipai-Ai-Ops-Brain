"""独立本机 PostgreSQL/Temporal 反证、门禁、幂等与历史回放。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import partial
from typing import Literal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities
from app.agent.client import LLMClient
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.models import ChatMessage, ChatRequest
from app.agent.reviewer.activities import ReviewerActivities
from app.agent.reviewer.models import ReviewDecision, ReviewRequest, ReviewResult
from app.agent.reviewer.scenario import review_response
from app.agent.workflow_models import InvestigationRequest
from app.config import Settings, parse_database_url
from app.db.base import utc_now
from app.db.session import Database
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.approval.activities import ApprovalActivities
from app.tasks.planning.activities import PlanningActivities
from app.tasks.review_gate import ReviewRequired
from app.tasks.safety.activities import SafetyActivities
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest, WorkflowInput
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.reviewer_fake import fake_review_registry
from tests.database_support import get_test_database_url, migrate
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import (
    accept,
    evidence_counts,
    investigating,
    new_task,
    runtime_settings,
    seed,
)
from tests.test_workflow_integration import wait_at

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="执行 check-reviewer.ps1 使用独立本机依赖",
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
        await seed(instance)
        yield instance
    finally:
        await instance.dispose()


async def prepared(database: Database) -> ReviewRequest:
    task = await investigating(database)
    result = await AgentActivities(database, Settings(APP_ENV="test")).investigate(
        InvestigationRequest(task, SPEC.model_dump_json())
    )
    conclusion_id = await accept(database, task, result)
    return ReviewRequest(
        TaskSnapshot(task.task_id, TaskStatus.RCA, task.version + 1),
        SPEC.model_dump_json(),
        conclusion_id,
    )


async def test_clear_review_real_evidence_confidence_and_planning_gate(database: Database) -> None:
    request = await prepared(database)
    store = TaskActivityStore(database)
    planning = TransitionRequest(request.task, TaskStatus.PLANNING, "Step 27 门禁验收")
    before = await evidence_counts(database, request.task.task_id)
    with pytest.raises(ReviewRequired):
        await store.transition(planning)
    assert await evidence_counts(database, request.task.task_id) == before
    result = await ReviewerActivities(database, Settings(APP_ENV="test")).review(request)
    decision = ReviewDecision.model_validate_json(result.decision_json)
    assert decision.original_confidence == 0.7 and decision.conclusion.confidence == 0.8
    assert decision.report.verdict == "clear" and decision.steps == 5
    async with database.session() as session:
        ledger = LedgerService(session)
        original = await ledger.get_evidence(UUID(request.conclusion_evidence_id))
        assert (
            isinstance(original.result_snapshot, dict)
            and original.result_snapshot["confidence"] == 0.7
        )
        for reference in decision.report.evidence_ids:
            assert (await ledger.get_evidence(reference)).task_id == UUID(request.task.task_id)
        assert (await ledger.get_evidence(UUID(result.evidence_id))).result_snapshot == json.loads(
            result.decision_json
        )
    assert (await store.transition(planning)).status is TaskStatus.PLANNING


@pytest.mark.parametrize("source", [TaskSource.ALERT, TaskSource.RELEASE])
async def test_critical_source_without_conclusion_cannot_plan(
    database: Database, source: TaskSource
) -> None:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(source=source, title="关键任务", reason="门禁测试")
        snapshot = TaskSnapshot(str(task.id), task.status, task.status_version)
    store = TaskActivityStore(database)
    for status in (
        TaskStatus.CONTEXT_BUILDING,
        TaskStatus.RUNBOOK_MATCHING,
        TaskStatus.INVESTIGATING,
        TaskStatus.RCA,
    ):
        snapshot = await store.transition(TransitionRequest(snapshot, status, "准备关键任务"))
    with pytest.raises(ReviewRequired):
        await store.transition(TransitionRequest(snapshot, TaskStatus.PLANNING, "跳过复核"))


async def test_counterexample_decreases_confidence_and_gate_rejects(database: Database) -> None:
    request = await prepared(database)
    reviewer = ReviewerActivities(
        database,
        Settings(APP_ENV="test"),
        registry_factory=partial(fake_review_registry, mode="contradicted"),
    )
    result = await reviewer.review(request)
    decision = ReviewDecision.model_validate_json(result.decision_json)
    assert decision.report.verdict == "contradicted" and decision.conclusion.confidence == 0.5
    with pytest.raises(ReviewRequired):
        await TaskActivityStore(database).transition(
            TransitionRequest(request.task, TaskStatus.PLANNING, "反证未处理")
        )


async def test_concurrent_commit_response_loss_reuses_same_checkpoints(database: Database) -> None:
    request = await prepared(database)
    reviewer = ReviewerActivities(database, Settings(APP_ENV="test"))
    first, second = await asyncio.gather(reviewer.review(request), reviewer.review(request))
    assert first == second
    counts = await evidence_counts(database, request.task.task_id)

    def no_llm(request: ChatRequest) -> LLMClient:
        raise AssertionError("已提交复核不能重新调用模型")

    retry = ReviewerActivities(database, Settings(APP_ENV="test"), llm_factory=no_llm)
    assert await retry.review(request) == first
    assert await evidence_counts(database, request.task.task_id) == counts
    assert counts == (22, 6)
    with pytest.raises(ApplicationError, match="被拒绝"):
        await retry.review(
            replace(
                request, spec_json=SPEC.model_copy(update={"title": "篡改输入"}).model_dump_json()
            )
        )


async def test_wrong_task_old_conclusion_and_checkpoint_ids_rejected(database: Database) -> None:
    request, other = await prepared(database), await prepared(database)
    reviewer = ReviewerActivities(database, Settings(APP_ENV="test"))
    for forged in (str(uuid4()), other.conclusion_evidence_id):
        with pytest.raises(ApplicationError, match="被拒绝"):
            await reviewer.review(replace(request, conclusion_evidence_id=forged))
    result = await reviewer.review(request)
    with pytest.raises(ApplicationError, match="被拒绝"):
        await reviewer.review(replace(request, conclusion_evidence_id=result.evidence_id))
    store = TaskActivityStore(database)
    task = await store.transition(
        TransitionRequest(request.task, TaskStatus.INVESTIGATING, "重新调查")
    )
    task = await store.transition(TransitionRequest(task, TaskStatus.RCA, "新 RCA 尚无复核"))
    with pytest.raises(ReviewRequired):
        await store.transition(TransitionRequest(task, TaskStatus.PLANNING, "复用旧复核"))


async def test_forged_report_references_rejected(database: Database) -> None:
    request = await prepared(database)

    def factory(value: ChatRequest) -> LLMClient:
        def response(value: ChatRequest):  # type: ignore[no-untyped-def]
            result = review_response(value)
            if result.message.content:
                data = json.loads(result.message.content)
                data["checks"][0]["evidence_ids"] = [request.conclusion_evidence_id]
                return result.model_copy(
                    update={"message": ChatMessage(role="assistant", content=json.dumps(data))}
                )
            return result

        return FakeLLM([ScriptedChatStep(response)])

    with pytest.raises(ApplicationError, match="被拒绝"):
        await ReviewerActivities(database, Settings(APP_ENV="test"), llm_factory=factory).review(
            request
        )
    async with database.session() as session:
        assert all(
            item.source_tool != "reviewer.verdict"
            for item in await LedgerService(session).evidence_for_task(UUID(request.task.task_id))
        )


async def test_policy_denied_review_is_audited_without_confidence_increase(
    database: Database,
) -> None:
    request = await prepared(database)
    settings = Settings(
        APP_ENV="test",
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "block-review-traces",
                    "action_names": ["query_traces"],
                    "risk_levels": ["L0"],
                    "environments": ["test"],
                    "decision": "deny",
                    "reason": "测试反证门禁",
                }
            ]
        },
    )
    with pytest.raises(ApplicationError):
        await ReviewerActivities(database, settings).review(request)
    async with database.session() as session:
        ledger = LedgerService(session)
        assert any(
            item.actor == "codex-reviewer" and item.outcome == "rejected"
            for item in await ledger.audits_for_task(UUID(request.task.task_id))
        )
    with pytest.raises(ReviewRequired):
        await TaskActivityStore(database).transition(
            TransitionRequest(request.task, TaskStatus.PLANNING, "查询拒绝后试图进入计划")
        )


async def test_observation_failure_rolls_back_evidence_and_audit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await prepared(database)
    before = await evidence_counts(database, request.task.task_id)
    original = LedgerService.append_evidence

    async def fail(self: LedgerService, **arguments: object) -> Evidence:
        if arguments["source_tool"] == "reviewer.observe":
            raise RuntimeError("观察保存失败")
        return await original(self, **arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(ApplicationError, match="复核失败"):
        await ReviewerActivities(database, Settings(APP_ENV="test")).review(request)
    assert await evidence_counts(database, request.task.task_id) == (before[0] + 1, before[1])


async def test_reviewer_tools_replay_original_results_without_connectors(
    database: Database,
) -> None:
    from app.policy.engine import create_policy_engine
    from app.tools.dispatcher import ToolDispatcher

    request = await prepared(database)
    result = await ReviewerActivities(database, Settings(APP_ENV="test")).review(request)
    decision = ReviewDecision.model_validate_json(result.decision_json)
    async with database.session() as session, session.begin():
        settings = Settings(APP_ENV="test")
        ledger = LedgerService(session)
        async with fake_review_registry(settings, session) as registry:
            dispatcher = ToolDispatcher(registry, create_policy_engine(settings), ledger)
            for reference in decision.report.evidence_ids:
                item = await ledger.get_evidence(reference)
                replay = await dispatcher.dispatch(
                    task_id=item.task_id,
                    tool_name=item.source_tool,
                    parameters=item.parameters,
                    actor="review-replay",
                    mode=DispatchMode.REPLAY,
                    replay_evidence_id=item.id,
                    replay_before=utc_now(),
                )
                assert (
                    replay.status is DispatchStatus.REPLAYED
                    and replay.result == item.result_snapshot
                )


def review_worker(
    client: Client, database: Database, settings: Settings, reviewer: ReviewerActivities
) -> Worker:
    tasks = TaskActivities(TaskActivityStore(database))
    agent = AgentActivities(database, settings)
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
            agent.investigate,
            agent.validate_conclusion,
            RunbookActivities(database, settings).match,
            reviewer.review,
            PlanningActivities(database, settings).plan,
            ApprovalActivities(database, settings).notify,
            ApprovalActivities(database, settings).decide,
        ],
        max_cached_workflows=0,
    )


@local_temporal
@pytest.mark.parametrize("mode", ["clear", "contradicted"])
async def test_temporal_verdict_branch_and_replay(
    database: Database, mode: Literal["clear", "contradicted"]
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    reviewer = ReviewerActivities(
        database, settings, registry_factory=partial(fake_review_registry, mode=mode)
    )
    handle = None
    try:
        async with review_worker(client, database, settings, reviewer):
            handle = await start_task_workflow(
                client,
                WorkflowInput(task.task_id, investigation_json=SPEC.model_dump_json()),
                task_queue=settings.temporal_config.task_queue,
            )
            if mode == "clear":
                await wait_at(handle, TaskStatus.WAITING_APPROVAL)
                progress = await handle.query(AITaskWorkflow.progress)
            else:
                progress = await asyncio.wait_for(handle.result(), 30)
        assert progress.review_json and progress.review_evidence_id
        decision = ReviewDecision.model_validate_json(progress.review_json)
        assert decision.conclusion.confidence == (0.8 if mode == "clear" else 0.5)
        states = [item.status for item in progress.history]
        if mode == "contradicted":
            assert states.count(TaskStatus.INVESTIGATING) == 1
            assert states.count(TaskStatus.RCA) == 1
            assert states[-1] is TaskStatus.AUTOMATION_ABORTED
            assert progress.safety_evidence_id and progress.takeover_notification_state == "sent"
        assert (TaskStatus.PLANNING in states) is (mode == "clear")
        assert TaskStatus.EXECUTING not in states
        async with database.session() as session:
            assert progress.conclusion_json and progress.conclusion_evidence_id
            ledger = LedgerService(session)
            original = await ledger.get_evidence(UUID(progress.conclusion_evidence_id))
            assert original.result_snapshot == json.loads(progress.conclusion_json)
            saved_review = await ledger.get_evidence(UUID(progress.review_evidence_id))
            assert saved_review.result_snapshot == json.loads(progress.review_json)
            history = await TaskService(session).history(UUID(task.task_id))
            assert [(item.to_status, item.sequence) for item in history] == [
                (item.status, item.version) for item in progress.history
            ]
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 27 验收清理")


@local_temporal
async def test_temporal_reviewer_commit_response_loss_restart_does_not_repeat(
    database: Database,
) -> None:
    settings, task = runtime_settings(), await new_task(database)
    client = await Client.connect(settings.temporal_config.address)
    model_calls = calls = 0
    committed = asyncio.Event()

    def factory(request: ChatRequest) -> LLMClient:
        nonlocal model_calls
        model_calls += 1
        return FakeLLM([ScriptedChatStep(review_response)])

    reviewer = ReviewerActivities(database, settings, llm_factory=factory)

    @activity.defn(name="reviewer.review")
    async def lose_response(request: ReviewRequest) -> ReviewResult:
        nonlocal calls
        result = await reviewer.review(request)
        calls += 1
        if calls == 1:
            committed.set()
            raise ApplicationError("复核提交后丢响应")
        return result

    def worker() -> Worker:
        tasks, agent = (
            TaskActivities(TaskActivityStore(database)),
            AgentActivities(database, settings),
        )
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
                agent.investigate,
                agent.validate_conclusion,
                RunbookActivities(database, settings).match,
                lose_response,
                PlanningActivities(database, settings).plan,
                ApprovalActivities(database, settings).notify,
                ApprovalActivities(database, settings).decide,
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
    async with worker():
        progress = await asyncio.wait_for(handle.result(), 30)
    assert progress.review_evidence_id and calls == 2 and model_calls == 3
    assert await evidence_counts(database, task.task_id) == (28, 7)
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


@pytest.mark.parametrize("escape", ["tool", "service", "window"])
async def test_reviewer_scope_and_tool_allowlist_reject_and_audit(
    database: Database, escape: str
) -> None:
    from app.agent.models import ChatResponse, FunctionCall, ToolCall

    request = await prepared(database)

    def factory(value: ChatRequest) -> LLMClient:
        def response(value: ChatRequest) -> ChatResponse:
            normal = review_response(value)
            call = normal.message.tool_calls[0]
            arguments = call.function.parsed_arguments
            name = call.function.name
            if escape == "tool":
                name = "get_service_context"
            elif escape == "service":
                arguments["service_name"] = "checkout-service"
            else:
                arguments["start"] = "2026-09-30T00:00:00Z"
            return normal.model_copy(
                update={
                    "message": ChatMessage(
                        role="assistant",
                        tool_calls=(
                            ToolCall(
                                id=call.id,
                                function=FunctionCall(name=name, arguments=json.dumps(arguments)),
                            ),
                        ),
                    )
                }
            )

        return FakeLLM([ScriptedChatStep(response)])

    with pytest.raises(ApplicationError):
        await ReviewerActivities(database, Settings(APP_ENV="test"), llm_factory=factory).review(
            request
        )
    async with database.session() as session:
        ledger = LedgerService(session)
        assert any(
            item.actor == "codex-reviewer" and item.outcome == "rejected"
            for item in await ledger.audits_for_task(UUID(request.task.task_id))
        )
        assert all(
            item.source_tool != "reviewer.verdict"
            for item in await ledger.evidence_for_task(UUID(request.task.task_id))
        )
