"""本机隔离 PostgreSQL/Temporal：问答、草稿、事务、恢复和回放。"""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities, configured_llm
from app.agent.client import LLMClient
from app.agent.models import ChatRequest
from app.agent.workflow_models import InvestigationRequest
from app.config import Settings, parse_database_url
from app.connectors.feishu.fake import FakeFeishuConnector
from app.db.session import Database
from app.knowledge.human_drafts import HumanKnowledgeDraft
from app.knowledge.models import KnowledgeEntry
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.runbooks.workflow_models import RunbookMatchRequest
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.human.activities import HumanActivities, HumanInteractionStore
from app.tasks.human.models import question_id
from app.tasks.safety.activities import SafetyActivities
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import (
    HumanAnswer,
    HumanAnswerRequest,
    HumanAnswerResult,
    HumanPrompt,
    HumanQuestion,
    HumanResponse,
    HumanWaitRequest,
    TaskSnapshot,
    TransitionRequest,
    WorkflowInput,
)
from app.verifier.placeholder import PlaceholderVerifier
from tests.database_support import get_test_database_url, migrate
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import seed
from tests.test_runbooks_integration import matching, save

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-human.ps1"
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


async def new_task(database: Database) -> str:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="人工问答验收", reason="Step 29 Fake"
        )
        return str(task.id)


async def waiting(database: Database, status: TaskStatus) -> HumanWaitRequest:
    task_id = await new_task(database)
    states = [TaskStatus.CONTEXT_BUILDING]
    if status is TaskStatus.NEED_HUMAN_JUDGMENT:
        states += [TaskStatus.RUNBOOK_MATCHING, TaskStatus.INVESTIGATING]
    resume = states[-1]
    states.append(status)
    async with database.session() as session, session.begin():
        service = TaskService(session)
        for version, state in enumerate(states):
            task = await service.transition(
                UUID(task_id),
                state,
                expected_version=version,
                reason="等待测试",
                expected_status=TaskStatus.NEW if version == 0 else states[version - 1],
            )
    return HumanWaitRequest(
        TaskSnapshot(task_id, task.status, task.status_version), "是否优先稳定性？", resume
    )


def response(prompt: HumanPrompt, text: str = "高峰期优先稳定性") -> HumanAnswerRequest:
    return HumanAnswerRequest(
        prompt,
        HumanAnswer(prompt.question_id, prompt.task.status, prompt.task.version, text, "owner"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION])
async def test_concurrent_notify_answer_and_cross_session_draft(
    database: Database, status: TaskStatus
) -> None:
    async with database.session() as session:
        published_count = await session.scalar(select(func.count()).select_from(KnowledgeEntry))
    request = await waiting(database, status)
    connector, store = FakeFeishuConnector(), HumanInteractionStore(database)
    prompts = await asyncio.gather(
        store.notify(request, connector), store.notify(request, connector)
    )
    assert prompts[0] == prompts[1] and len(connector.sent_messages) == 1
    answers = await asyncio.gather(
        store.answer(response(prompts[0])), store.answer(response(prompts[0]))
    )
    assert answers[0] == answers[1]
    async with database.session() as session:
        draft = await session.get(HumanKnowledgeDraft, UUID(answers[0].knowledge_draft_id))
        assert (
            draft is not None
            and draft.status == "draft"
            and str(draft.task_id) == request.task.task_id
        )
        assert draft.answer == "高峰期优先稳定性" and draft.respondent == "owner"
        assert draft.created_at.tzinfo is UTC
        ledger = LedgerService(session)
        evidence = await ledger.get_evidence(draft.answer_evidence_id)
        assert evidence.source_tool == "human.answer" and evidence.task_id == draft.task_id
        audits = [
            a
            for a in await ledger.audits_for_task(draft.task_id)
            if a.event_type is AuditEventType.HUMAN_INTERACTION
        ]
        assert len(audits) == 2 and audits[-1].actor == "owner"
        assert (
            await session.scalar(select(func.count()).select_from(KnowledgeEntry))
            == published_count
        )


@pytest.mark.asyncio
async def test_conflicting_question_and_answer_rejected(database: Database) -> None:
    request = await waiting(database, TaskStatus.NEED_HUMAN_JUDGMENT)
    store, connector = HumanInteractionStore(database), FakeFeishuConnector()
    prompt = await store.notify(request, connector)
    with pytest.raises(ValueError, match="改写"):
        await store.notify(replace(request, question="新问题"), connector)
    await store.answer(response(prompt))
    with pytest.raises(ValueError, match="其他回答"):
        await store.answer(response(prompt, "降低成本"))


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["question_id", "wait_version", "evidence", "task"])
async def test_forged_or_cross_task_answer_rejected(database: Database, tamper: str) -> None:
    request = await waiting(database, TaskStatus.NEED_HUMAN_JUDGMENT)
    store = HumanInteractionStore(database)
    prompt = await store.notify(request, FakeFeishuConnector())
    value = response(prompt)
    if tamper == "question_id":
        value = replace(value, response=replace(value.response, question_id=str(uuid4())))
    elif tamper == "wait_version":
        value = replace(value, response=replace(value.response, wait_version=3))
    elif tamper == "evidence":
        other = await store.notify(
            await waiting(database, TaskStatus.NEED_HUMAN_JUDGMENT), FakeFeishuConnector()
        )
        value = replace(
            value, prompt=replace(prompt, question_evidence_id=other.question_evidence_id)
        )
    else:
        forged = replace(prompt, task=replace(prompt.task, task_id=await new_task(database)))
        forged = replace(forged, question_id=question_id(forged.task))
        value = response(forged)
    with pytest.raises((ValueError, LookupError)):
        await store.answer(value)


@pytest.mark.asyncio
async def test_stale_answer_rejected_and_committed_retry_reused(database: Database) -> None:
    request = await waiting(database, TaskStatus.WAITING_INFORMATION)
    store = HumanInteractionStore(database)
    prompt = await store.notify(request, FakeFeishuConnector())
    async with database.session() as session, session.begin():
        await TaskService(session).transition(
            UUID(request.task.task_id),
            TaskStatus.ESCALATED,
            expected_status=request.task.status,
            expected_version=request.task.version,
            reason="超时",
        )
    with pytest.raises(ValueError, match="过期"):
        await store.answer(response(prompt))
    request = await waiting(database, TaskStatus.WAITING_INFORMATION)
    prompt = await store.notify(request, FakeFeishuConnector())
    first = await store.answer(response(prompt))
    async with database.session() as session, session.begin():
        await TaskService(session).transition(
            UUID(request.task.task_id),
            request.resume_status,
            expected_status=request.task.status,
            expected_version=request.task.version,
            reason="恢复",
        )
    assert await store.answer(response(prompt)) == first
    # 旧版无法表达人工问答审计；拒绝降级并保持当前 head 和草稿。
    with pytest.raises(subprocess.CalledProcessError):
        migrate("downgrade", "0010_runbook_engine")
    async with database.session() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0016_catalog_audit"
        )
        assert await session.get(HumanKnowledgeDraft, UUID(first.knowledge_draft_id)) is not None


@pytest.mark.asyncio
async def test_answer_audit_failure_rolls_back_evidence_and_draft(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await waiting(database, TaskStatus.WAITING_INFORMATION)
    store = HumanInteractionStore(database)
    prompt = await store.notify(request, FakeFeishuConnector())
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> object:
        raise RuntimeError("模拟审计失败")

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError):
        await store.answer(response(prompt))
    async with database.session() as session:
        assert (
            await session.scalar(
                select(HumanKnowledgeDraft).where(
                    HumanKnowledgeDraft.task_id == UUID(request.task.task_id)
                )
            )
            is None
        )
        assert len(await LedgerService(session).evidence_for_task(UUID(request.task.task_id))) == 1
    monkeypatch.setattr(LedgerService, "append_audit", original)
    await store.answer(response(prompt))


@pytest.mark.asyncio
async def test_notification_sent_then_transaction_failure_deduplicates(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await waiting(database, TaskStatus.WAITING_INFORMATION)
    connector, store = FakeFeishuConnector(), HumanInteractionStore(database)
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> object:
        raise RuntimeError("发送后事务失败")

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError):
        await store.notify(request, connector)
    assert len(connector.sent_messages) == 1
    monkeypatch.setattr(LedgerService, "append_audit", original)
    await store.notify(request, connector)
    assert len(connector.sent_messages) == 1
    async with database.session() as session:
        assert len(await LedgerService(session).evidence_for_task(UUID(request.task.task_id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("use_runbook", [False, True])
async def test_persisted_answer_reaches_resumed_agent_and_retry_context_is_stable(
    database: Database,
    use_runbook: bool,
) -> None:
    await seed(database)
    guide = None
    if use_runbook:
        await save(database, name=f"human-guidance-{uuid4().hex}")
        phase = await matching(database)
        match = await RunbookActivities(database, Settings(APP_ENV="test")).match(
            RunbookMatchRequest(phase, SPEC.model_dump_json())
        )
        guide = match.runbook_json
        assert guide is not None
        phases = TaskActivityStore(database)
        phase = await phases.transition(
            TransitionRequest(phase, TaskStatus.INVESTIGATING, "进入调查")
        )
        phase = await phases.transition(
            TransitionRequest(phase, TaskStatus.NEED_HUMAN_JUDGMENT, "业务判断")
        )
        request = HumanWaitRequest(phase, "高峰期优先稳定性还是成本？", TaskStatus.INVESTIGATING)
    else:
        request = await waiting(database, TaskStatus.NEED_HUMAN_JUDGMENT)
    store = HumanInteractionStore(database)
    prompt = await store.notify(request, FakeFeishuConnector())
    answer_result = await store.answer(response(prompt))
    task = await TaskActivityStore(database).transition(
        TransitionRequest(request.task, request.resume_status, "回答后恢复调查")
    )
    settings = Settings(APP_ENV="test")
    calls: list[ChatRequest] = []

    def llm_factory(chat: ChatRequest) -> LLMClient:
        calls.append(chat)
        assert any(
            answer_result.answer_evidence_id in (message.content or "")
            and "高峰期优先稳定性" in (message.content or "")
            for message in chat.messages
        )
        return configured_llm(settings, chat)

    agent = AgentActivities(database, settings, llm_factory=llm_factory)
    investigation = InvestigationRequest(task, SPEC.model_dump_json(), guide)
    first = await agent.investigate(investigation)
    assert await agent.investigate(investigation) == first
    assert len(calls) == (1 if use_runbook else 5) and len(first.observed_ids) == 4


temporal_test = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需要本机 Temporal"
)


async def wait_prompt(handle: object) -> HumanPrompt:
    from temporalio.client import WorkflowHandle

    assert isinstance(handle, WorkflowHandle)
    async with asyncio.timeout(20):
        while True:
            progress = await handle.query(AITaskWorkflow.progress)
            if progress.human_prompt is not None:
                return progress.human_prompt
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
@temporal_test
@pytest.mark.parametrize("status", [TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION])
async def test_signal_worker_restart_resume_and_replay(
    database: Database, status: TaskStatus
) -> None:
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"human-{uuid4().hex}",
        },
    )
    connector = FakeFeishuConnector()
    value = WorkflowInput(
        await new_task(database),
        human_questions=[HumanQuestion(status, "业务优先级是什么？")],
        human_timeout_seconds=60,
    )
    async with create_worker(
        client, database, settings, feishu_connector=connector, max_cached_workflows=0
    ):
        handle = await start_task_workflow(
            client, value, task_queue=settings.temporal_config.task_queue
        )
        prompt = await wait_prompt(handle)
        assert len(connector.sent_messages) == 1
        await handle.signal(
            AITaskWorkflow.human_response, HumanResponse(status, prompt.task.version, True)
        )
        await handle.signal(
            AITaskWorkflow.answer_question,
            replace(response(prompt).response, question_id=str(uuid4())),
        )
        assert (await handle.query(AITaskWorkflow.progress)).task == prompt.task
    await handle.signal(AITaskWorkflow.answer_question, response(prompt).response)
    await handle.signal(AITaskWorkflow.answer_question, response(prompt, "重复回答").response)
    async with create_worker(
        client, database, settings, feishu_connector=connector, max_cached_workflows=0
    ):
        result = await asyncio.wait_for(handle.result(), 30)
    assert result.task and result.task.status is TaskStatus.CLOSED
    assert len(result.human_answers) == 1 and len(connector.sent_messages) == 1
    async with database.session() as session:
        history = await TaskService(session).history(UUID(value.task_id))
        assert [(h.to_status, h.sequence) for h in history] == [
            (s.status, s.version) for s in result.history
        ]
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


@pytest.mark.asyncio
@temporal_test
async def test_timeout_does_not_execute_or_create_draft(database: Database) -> None:
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"human-timeout-{uuid4().hex}",
        },
    )
    value = WorkflowInput(
        await new_task(database),
        human_questions=[HumanQuestion(TaskStatus.NEED_HUMAN_JUDGMENT, "需要判断")],
        human_timeout_seconds=0.1,
    )
    async with create_worker(client, database, settings):
        handle = await start_task_workflow(
            client, value, task_queue=settings.temporal_config.task_queue
        )
        result = await asyncio.wait_for(handle.result(), 30)
    assert result.task and result.task.status is TaskStatus.ESCALATED
    assert not result.human_answers
    assert TaskStatus.EXECUTING not in [s.status for s in result.history]


@pytest.mark.asyncio
@temporal_test
async def test_temporal_commit_then_lost_response_deduplicates(database: Database) -> None:
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    queue = f"human-retry-{uuid4().hex}"
    tasks = TaskActivities(TaskActivityStore(database))
    human = HumanActivities(database, Settings(APP_ENV="test"), connector=FakeFeishuConnector())
    calls = 0

    @activity.defn(name="human.record_answer")
    async def lose_response(request: HumanAnswerRequest) -> HumanAnswerResult:
        nonlocal calls
        result = await human.record_answer(request)
        calls += 1
        if calls == 1:
            raise ApplicationError("提交后丢失响应")
        return result

    value = WorkflowInput(
        await new_task(database),
        human_questions=[HumanQuestion(TaskStatus.WAITING_INFORMATION, "请补充规则")],
    )
    async with Worker(
        client,
        task_queue=queue,
        workflows=[AITaskWorkflow],
        activities=[
            SafetyActivities(database, Settings(APP_ENV="test")).check,
            SafetyActivities(database, Settings(APP_ENV="test")).notify,
            tasks.load,
            tasks.transition,
            tasks.placeholder_stage,
            PlaceholderVerifier(TaskActivityStore(database), app_env="test").verify,
            human.notify,
            lose_response,
        ],
    ):
        handle = await start_task_workflow(client, value, task_queue=queue)
        await handle.signal(
            AITaskWorkflow.answer_question, response(await wait_prompt(handle)).response
        )
        result = await asyncio.wait_for(handle.result(), 30)
    assert calls == 2 and len(result.human_answers) == 1
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
