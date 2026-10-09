"""Step 24：独立本机 PostgreSQL/Temporal，所有运维数据与 LLM 均为 Fake。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities, configured_llm
from app.agent.client import LLMClient
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import AgentConclusion, InvestigationResult
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.agent.reviewer.activities import ReviewerActivities
from app.agent.scenario import PAYMENT_TOOLS, invalid_conclusion_response
from app.agent.workflow_models import ConclusionRequest, InvestigationRequest
from app.config import Settings, parse_database_url
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.approval.activities import ApprovalActivities
from app.tasks.models import AITask
from app.tasks.planning.activities import PlanningActivities
from app.tasks.safety.activities import SafetyActivities
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanResponse, TaskSnapshot, TransitionRequest, WorkflowInput
from app.triggers.activities import EventActivities
from app.triggers.schemas import NormalizedEvent
from app.triggers.service import EventService
from tests.database_support import get_test_database_url, migrate
from tests.test_main_agent import END, SPEC

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-agent.ps1 使用隔离本机依赖"
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
        await seed(database)
        yield database
    finally:
        await database.dispose()


async def seed(database: Database) -> None:
    settings = Settings(APP_ENV="test")
    await DiscoveryActivities(database, settings).refresh(DiscoveryRequest(END.isoformat(), 3600))
    await TimelineActivities(database, settings).collect(
        TimelineRequest("payment-service", SPEC.start.isoformat(), SPEC.end.isoformat())
    )


async def new_task(database: Database) -> TaskSnapshot:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.ALERT, title=SPEC.title, reason="Step 24 Fake 验收"
        )
    return TaskSnapshot(str(task.id), task.status, task.status_version)


async def investigating(database: Database) -> TaskSnapshot:
    task = await new_task(database)
    store = TaskActivityStore(database)
    for status in (
        TaskStatus.CONTEXT_BUILDING,
        TaskStatus.RUNBOOK_MATCHING,
        TaskStatus.INVESTIGATING,
    ):
        task = await store.transition(TransitionRequest(task, status, "Step 24 准备阶段"))
    return task


async def accept(database: Database, task: TaskSnapshot, result: InvestigationResult) -> str:
    rca = await TaskActivityStore(database).transition(
        TransitionRequest(task, TaskStatus.RCA, "主 Agent 调查完成，进入证据校验")
    )
    return await AgentActivities(database, Settings(APP_ENV="test")).validate_conclusion(
        ConclusionRequest(rca, result)
    )


async def evidence_counts(database: Database, task_id: str) -> tuple[int, int]:
    async with database.session() as session:
        ledger = LedgerService(session)
        return len(await ledger.evidence_for_task(UUID(task_id))), len(
            [
                audit
                for audit in await ledger.audits_for_task(UUID(task_id))
                if audit.event_type is AuditEventType.TOOL_CALL
            ]
        )


@pytest.mark.asyncio
async def test_payment_real_evidence_and_idempotent_accept(database: Database) -> None:
    task = await investigating(database)
    activities = AgentActivities(database, Settings(APP_ENV="test"))
    result = await activities.investigate(InvestigationRequest(task, SPEC.model_dump_json()))
    conclusion_id = await accept(database, task, result)
    async with database.session() as session:
        ledger = LedgerService(session)
        evidence = await ledger.evidence_for_task(UUID(task.task_id))
        tool_evidence = [item for item in evidence if item.source_tool in PAYMENT_TOOLS]
        assert [item.source_tool for item in tool_evidence] == list(PAYMENT_TOOLS)
        conclusion = AgentConclusion.model_validate_json(result.conclusion_json)
        assert conclusion.evidence_ids == {item.id for item in tool_evidence}
        saved = await ledger.get_evidence(UUID(conclusion_id))
        assert saved.result_snapshot == json.loads(result.conclusion_json)
        rca_task = await session.get(AITask, UUID(task.task_id))
        assert rca_task is not None
        rca = TaskSnapshot(task.task_id, rca_task.status, rca_task.status_version)
    assert await activities.validate_conclusion(ConclusionRequest(rca, result)) == conclusion_id
    assert await evidence_counts(database, task.task_id) == (14, 4)


@pytest.mark.asyncio
async def test_retry_and_concurrent_attempts_reuse_committed_llm_tools(database: Database) -> None:
    task = await investigating(database)
    activities = AgentActivities(database, Settings(APP_ENV="test"))
    request = InvestigationRequest(task, SPEC.model_dump_json())
    first, concurrent = await asyncio.gather(
        activities.investigate(request), activities.investigate(request)
    )
    assert first == concurrent

    def no_llm(request: ChatRequest) -> LLMClient:
        raise AssertionError("重试不应重新调用已提交 LLM")

    retry = AgentActivities(database, Settings(APP_ENV="test"), llm_factory=no_llm)
    assert await retry.investigate(request) == first
    assert await evidence_counts(database, task.task_id) == (13, 4)
    with pytest.raises(ApplicationError, match="被拒绝"):
        await retry.investigate(
            replace(
                request,
                spec_json=SPEC.model_copy(update={"title": "不同的调查请求"}).model_dump_json(),
            )
        )


@pytest.mark.asyncio
async def test_forged_cross_task_and_checkpoint_ids_are_rejected(database: Database) -> None:
    activities = AgentActivities(database, Settings(APP_ENV="test"))
    task = await investigating(database)
    result = await activities.investigate(InvestigationRequest(task, SPEC.model_dump_json()))
    rca = await TaskActivityStore(database).transition(
        TransitionRequest(task, TaskStatus.RCA, "准备引用校验")
    )
    other = await investigating(database)
    other_result = await activities.investigate(InvestigationRequest(other, SPEC.model_dump_json()))
    async with database.session() as session:
        checkpoint = next(
            item
            for item in await LedgerService(session).evidence_for_task(UUID(task.task_id))
            if item.source_tool == "agent.think"
        )
    for forged_id in (str(uuid4()), other_result.observed_ids[0], str(checkpoint.id)):
        data = json.loads(result.conclusion_json)
        data["root_cause"]["evidence_ids"] = [forged_id]
        forged = replace(result, conclusion_json=json.dumps(data))
        with pytest.raises(ApplicationError, match="校验失败"):
            await activities.validate_conclusion(ConclusionRequest(rca, forged))
        forged = replace(forged, observed_ids=result.observed_ids + [forged_id])
        with pytest.raises(ApplicationError, match="校验失败"):
            await activities.validate_conclusion(ConclusionRequest(rca, forged))
    async with database.session() as session:
        assert all(
            item.source_tool != "agent.conclusion"
            for item in await LedgerService(session).evidence_for_task(UUID(task.task_id))
        )


@pytest.mark.asyncio
async def test_policy_denial_is_audited_and_not_cited(database: Database) -> None:
    settings = Settings(
        APP_ENV="test",
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "block-logs",
                    "action_names": ["query_logs"],
                    "risk_levels": ["L0"],
                    "environments": ["test"],
                    "decision": "deny",
                    "reason": "验收拒绝日志查询",
                }
            ]
        },
    )
    task = await investigating(database)
    with pytest.raises(ApplicationError, match="被拒绝"):
        await AgentActivities(database, settings).investigate(
            InvestigationRequest(task, SPEC.model_dump_json())
        )
    async with database.session() as session:
        ledger = LedgerService(session)
        evidence = await ledger.evidence_for_task(UUID(task.task_id))
        assert all(item.source_tool not in {"query_logs", "agent.conclusion"} for item in evidence)
        audits = await ledger.audits_for_task(UUID(task.task_id))
        assert any(item.operation == "query_logs" and item.outcome == "rejected" for item in audits)


@pytest.mark.asyncio
async def test_llm_cannot_execute_a_write_or_assign_its_risk(database: Database) -> None:
    def factory(request: ChatRequest) -> LLMClient:
        if any(message.role == "tool" for message in request.messages):
            return FakeLLM(
                [
                    ScriptedChatStep(
                        lambda value: ChatResponse(
                            id="refused",
                            model="fake",
                            finish_reason="stop",
                            message=ChatMessage(role="assistant", refusal="没有放行的动作证据"),
                        )
                    )
                ]
            )
        return FakeLLM(
            [
                ScriptedChatStep(
                    lambda value: ChatResponse(
                        id="write-attempt",
                        model="fake",
                        finish_reason="tool_calls",
                        message=ChatMessage(
                            role="assistant",
                            tool_calls=(
                                ToolCall(
                                    id="write",
                                    function=FunctionCall(
                                        name="execute_action",
                                        arguments=json.dumps(
                                            {
                                                "service_name": "payment-service",
                                                "approved": True,
                                                "risk_level": "L0",
                                            }
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    )
                )
            ]
        )

    task = await investigating(database)
    with pytest.raises(ApplicationError, match="被拒绝"):
        await AgentActivities(database, Settings(APP_ENV="test"), llm_factory=factory).investigate(
            InvestigationRequest(task, SPEC.model_dump_json())
        )
    async with database.session() as session:
        ledger = LedgerService(session)
        audit = next(
            item
            for item in await ledger.audits_for_task(UUID(task.task_id))
            if item.event_type is AuditEventType.TOOL_CALL
        )
        assert audit.operation == "execute_action" and audit.outcome == "rejected"
        policy = audit.details["policy"]
        assert isinstance(policy, dict) and policy["risk_level"] == "L5"
        assert all(
            item.source_tool != "execute_action"
            for item in await ledger.evidence_for_task(UUID(task.task_id))
        )


@pytest.mark.asyncio
async def test_failed_observation_write_rolls_back_tool_evidence_and_audit(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = await investigating(database)
    original = LedgerService.append_evidence

    async def fail_observation(self: LedgerService, **arguments: object) -> Evidence:
        if arguments["source_tool"] == "agent.observe":
            raise RuntimeError("模拟观察保存失败")
        return await original(self, **arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_evidence", fail_observation)
    with pytest.raises(ApplicationError, match="调查失败"):
        await AgentActivities(database, Settings(APP_ENV="test")).investigate(
            InvestigationRequest(task, SPEC.model_dump_json())
        )
    assert await evidence_counts(database, task.task_id) == (1, 0)


def runtime_settings() -> Settings:
    return Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"agent-{uuid4().hex}",
        },
    )


def agent_worker(
    client: Client, database: Database, settings: Settings, agent: AgentActivities | None = None
) -> Worker:
    tasks = TaskActivities(TaskActivityStore(database))
    agent = agent or AgentActivities(database, settings)
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
            ReviewerActivities(database, settings).review,
            PlanningActivities(database, settings).plan,
            ApprovalActivities(database, settings).notify,
            ApprovalActivities(database, settings).decide,
            RunbookActivities(database, settings).match,
        ],
        max_cached_workflows=0,
    )


@local_temporal
@pytest.mark.asyncio
async def test_temporal_event_entry_rca_restart_signal_and_replay(database: Database) -> None:
    from tests.test_workflow_integration import wait_at

    settings = runtime_settings().model_copy(
        update={
            "agent_config": Settings(APP_ENV="test", AGENT_CONFIG={"enabled": True}).agent_config
        }
    )
    client = await Client.connect(settings.temporal_config.address)
    async with database.session() as session, session.begin():
        receipts = await EventService(session).accept(
            [
                NormalizedEvent(
                    origin="prometheus",
                    source=TaskSource.ALERT,
                    external_id=f"agent-{uuid4().hex}",
                    service_name="payment-service",
                    title=SPEC.title,
                    occurred_at=END,
                )
            ]
        )
    receipt = receipts[0]
    handle = client.get_workflow_handle(receipt.workflow_id)
    try:
        async with agent_worker(client, database, settings):
            await EventActivities(database, settings, client).start_task(receipt)
            snapshot = await wait_at(handle, TaskStatus.WAITING_APPROVAL)
            from tests.test_approval_integration import signal_value, wait_prompt

            prompt = await wait_prompt(handle)
            progress = await handle.query(AITaskWorkflow.progress)
            assert progress.conclusion_json and progress.conclusion_evidence_id
            assert [item.status for item in progress.history] == [
                TaskStatus.NEW,
                TaskStatus.CONTEXT_BUILDING,
                TaskStatus.RUNBOOK_MATCHING,
                TaskStatus.INVESTIGATING,
                TaskStatus.RCA,
                TaskStatus.PLANNING,
                TaskStatus.WAITING_APPROVAL,
            ]
        await handle.signal(
            AITaskWorkflow.human_response, HumanResponse(snapshot.status, snapshot.version, True)
        )
        await handle.signal(AITaskWorkflow.approve_actions, signal_value(prompt, "rejected"))
        async with agent_worker(client, database, settings):
            await asyncio.wait_for(handle.result(), 30)
        async with database.session() as session:
            task = await session.get(AITask, UUID(receipt.task_id))
            assert task is not None and task.status is TaskStatus.ESCALATED
            history = await TaskService(session).history(task.id)
            assert TaskStatus.EXECUTING not in [entry.to_status for entry in history]
        assert await evidence_counts(database, receipt.task_id) == (28, 7)
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        description = await handle.describe()
        if description.status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("验收清理")


@local_temporal
@pytest.mark.asyncio
async def test_unbound_kubernetes_event_waits_for_context_instead_of_staying_new(
    database: Database,
) -> None:
    from tests.test_workflow_integration import wait_at

    settings = runtime_settings().model_copy(
        update={
            "agent_config": Settings(APP_ENV="test", AGENT_CONFIG={"enabled": True}).agent_config
        }
    )
    client = await Client.connect(settings.temporal_config.address)
    async with database.session() as session, session.begin():
        receipt = (
            await EventService(session).accept(
                [
                    NormalizedEvent(
                        origin="kubernetes",
                        source=TaskSource.ALERT,
                        external_id=f"unbound-{uuid4().hex}",
                        service_name="default/Pod/unbound-pod",
                        title="未关联服务的 K8s Warning",
                        occurred_at=END,
                    )
                ]
            )
        )[0]
    handle = client.get_workflow_handle(receipt.workflow_id)
    try:
        async with agent_worker(client, database, settings):
            await EventActivities(database, settings, client).start_task(receipt)
            snapshot = await wait_at(handle, TaskStatus.WAITING_INFORMATION)
            assert snapshot.version == 2
            progress = await handle.query(AITaskWorkflow.progress)
            assert progress.conclusion_json is None
            assert TaskStatus.INVESTIGATING not in [item.status for item in progress.history]
    finally:
        if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("未关联服务验收清理")


@local_temporal
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid", "limit"])
async def test_temporal_invalid_conclusion_and_limit_escalate(
    database: Database, mode: str
) -> None:
    from app.tasks.worker import start_task_workflow

    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    spec = SPEC.model_copy(update={"max_steps": 3}) if mode == "limit" else SPEC

    def factory(request: ChatRequest) -> LLMClient:
        index = sum(message.role == "tool" for message in request.messages)
        if mode == "invalid" and index == 4:
            return FakeLLM([ScriptedChatStep(invalid_conclusion_response)])
        return configured_llm(settings, request)

    agent = AgentActivities(database, settings, llm_factory=factory)
    async with agent_worker(client, database, settings, agent):
        handle = await start_task_workflow(
            client,
            WorkflowInput(task.task_id, investigation_json=spec.model_dump_json()),
            task_queue=settings.temporal_config.task_queue,
        )
        await asyncio.wait_for(handle.result(), 30)
    async with database.session() as session:
        persisted = await session.get(AITask, UUID(task.task_id))
        assert persisted is not None and persisted.status is TaskStatus.ESCALATED
        history = await TaskService(session).history(persisted.id)
        assert all(
            entry.to_status not in {TaskStatus.PLANNING, TaskStatus.EXECUTING, TaskStatus.RESOLVED}
            for entry in history
        )
        if mode == "limit":
            assert "最大调查步数" in history[-1].reason
    assert await evidence_counts(database, task.task_id) == ((6, 2) if mode == "limit" else (15, 5))
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


@local_temporal
@pytest.mark.asyncio
async def test_temporal_commit_response_loss_restart_and_conclusion_retry(
    database: Database,
) -> None:
    from app.tasks.worker import start_task_workflow

    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    llm_calls = 0
    investigation_calls = 0
    conclusion_calls = 0
    committed = asyncio.Event()

    def factory(request: ChatRequest) -> LLMClient:
        nonlocal llm_calls
        llm_calls += 1
        return configured_llm(settings, request)

    agent = AgentActivities(database, settings, llm_factory=factory)
    tasks = TaskActivities(TaskActivityStore(database))

    @activity.defn(name="agent.investigate")
    async def lost_investigation(request: InvestigationRequest) -> InvestigationResult:
        nonlocal investigation_calls
        result = await agent.investigate(request)
        investigation_calls += 1
        if investigation_calls == 1:
            committed.set()
            raise ApplicationError("模拟调查提交后丢响应")
        return result

    @activity.defn(name="agent.validate_conclusion")
    async def lost_conclusion(request: ConclusionRequest) -> str:
        nonlocal conclusion_calls
        result = await agent.validate_conclusion(request)
        conclusion_calls += 1
        if conclusion_calls == 1:
            raise ApplicationError("模拟结论提交后丢响应")
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
                lost_investigation,
                lost_conclusion,
                ReviewerActivities(database, settings).review,
                PlanningActivities(database, settings).plan,
                ApprovalActivities(database, settings).notify,
                ApprovalActivities(database, settings).decide,
                RunbookActivities(database, settings).match,
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
        await asyncio.wait_for(handle.result(), 30)
    assert investigation_calls == 2 and conclusion_calls == 2 and llm_calls == 5
    assert await evidence_counts(database, task.task_id) == (28, 7)
    async with database.session() as session:
        history = await TaskService(session).history(UUID(task.task_id))
        assert [item.to_status for item in history] == [
            TaskStatus.NEW,
            TaskStatus.CONTEXT_BUILDING,
            TaskStatus.RUNBOOK_MATCHING,
            TaskStatus.INVESTIGATING,
            TaskStatus.RCA,
            TaskStatus.PLANNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.ESCALATED,
        ]
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
