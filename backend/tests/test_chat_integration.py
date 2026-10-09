"""本机隔离 PostgreSQL/Temporal：真实 SSE、引用、Policy 与幂等恢复。"""

import asyncio
import json
import os
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

from app.agent.activities import configured_llm
from app.agent.chat.activities import ChatActivities
from app.agent.chat.gateway import ChatGateway
from app.agent.chat.models import ChatInput, ChatSubmission
from app.agent.chat.service import chat_input, submit_chat
from app.agent.client import LLMClient
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.models import ChatRequest
from app.agent.scenario import invalid_conclusion_response
from app.api.chat import get_chat_gateway
from app.config import Settings
from app.db.session import Database
from app.ledger.service import LedgerService
from app.tasks.console_queries import ConsoleConflict, ConsoleNotFound
from app.tasks.models import AITask
from app.tasks.states import TaskSource
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.triggers.schemas import EventReceipt
from tests.test_chat import body, parse_events
from tests.test_console_integration import api, http
from tests.test_reviewer_integration import database, migrated_schema

__all__ = ["api", "http", "database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="运行 check-chat.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


def runtime_settings(**changes: object) -> Settings:
    return Settings.model_validate(
        {
            "APP_ENV": "test",
            "TEMPORAL_CONFIG": {
                "address": os.environ.get("TEST_TEMPORAL_ADDRESS", "127.0.0.1:7233"),
                "task_queue": f"chat-test-{uuid4().hex}",
            },
            **changes,
        }
    )


def submission(**changes: object) -> ChatSubmission:
    return ChatSubmission(
        input=ChatInput.model_validate_json(json.dumps(body(**changes))),
        actor="local-owner",
    )


async def test_input_concurrent_retry_conflict_and_actor_audit(database: Database) -> None:
    value = submission()

    async def accept() -> str:
        async with database.session() as session, session.begin():
            return (await submit_chat(session, value)).task_id

    first, second = await asyncio.gather(accept(), accept())
    assert first == second
    async with database.session() as session, session.begin():
        with pytest.raises(ConsoleConflict):
            await submit_chat(
                session,
                value.model_copy(
                    update={"input": value.input.model_copy(update={"message": "修改请求"})}
                ),
            )
    async with database.session() as session:
        record, saved = await chat_input(session, UUID(first))
        assert saved == value
        audits = [
            a
            for a in await LedgerService(session).audits_for_task(UUID(first))
            if a.operation == "chat.submit"
        ]
        assert len(audits) == 1 and audits[0].actor == "local-owner"
        assert audits[0].evidence_id == record.id and audits[0].occurred_at.tzinfo is not None


async def test_missing_previous_and_pending_followup_do_not_create_task(database: Database) -> None:
    with pytest.raises(ConsoleNotFound):
        async with database.session() as session, session.begin():
            await submit_chat(session, submission(previous_task_id=str(uuid4())))
    async with database.session() as session, session.begin():
        first = await submit_chat(session, submission())
    with pytest.raises(ConsoleConflict):
        async with database.session() as session, session.begin():
            await submit_chat(session, submission(previous_task_id=first.task_id))


@local_temporal
@pytest.mark.parametrize("mode", ["question", "task"])
async def test_real_sse_human_task_policy_and_reconnect(
    database: Database, api: FastAPI, http: httpx.AsyncClient, mode: str
) -> None:
    settings = runtime_settings(EXECUTION_CONFIG={"enabled": True})
    client = await Client.connect(settings.temporal_config.address)
    gateway = ChatGateway(client, database, settings)
    api.dependency_overrides[get_chat_gateway] = lambda: gateway
    request = body(mode=mode, message="请调查并回滚 payment-service v2.3.7 到 v2.3.6")
    handle = None
    try:
        async with create_worker(client, database, settings):
            response = await http.post("/api/chat", json=request)
            assert response.status_code == 200, response.text
            events = parse_events(response.text)
            assert [e[0] for e in events][:2] == ["task", "evidence"], response.text
            done = events[-1][1]
            assert done["status"] == ("CLOSED" if mode == "question" else "WAITING_APPROVAL")
            assert done["policy_decision"] == (None if mode == "question" else "need_approval")
            task_id = UUID(str(done["task_id"]))
            handle = client.get_workflow_handle_for(AITaskWorkflow.run, f"ai-task-{task_id}")
            async with database.session() as session:
                task = await session.get(AITask, task_id)
                assert task is not None and task.source is TaskSource.HUMAN
                records = await LedgerService(session).evidence_for_task(task_id)
                assert not any(e.source_tool.startswith("executor.") for e in records)
                for evidence_id in done["evidence_ids"]:
                    evidence = await LedgerService(session).get_evidence(UUID(str(evidence_id)))
                    assert evidence.task_id == task_id
            repeated = parse_events((await http.post("/api/chat", json=request)).text)
            assert repeated[0][1]["duplicate"] is True
            assert repeated[-1][1] == done
            assert (await http.get(f"/api/chat/{task_id}")).json() == done
            assert (
                await http.post("/api/chat", json={**request, "message": "换个请求"})
            ).status_code == 409
            if mode == "question":
                follow = body(previous_task_id=str(task_id), message="哪些证据支持这个判断？")
                followed = parse_events((await http.post("/api/chat", json=follow)).text)[-1][1]
                assert followed["status"] == "CLOSED" and followed["task_id"] != str(task_id)
                assert set(followed["evidence_ids"]).isdisjoint(done["evidence_ids"])
                follow_handle = client.get_workflow_handle_for(
                    AITaskWorkflow.run, f"ai-task-{followed['task_id']}"
                )
                await follow_handle.result()
            else:
                plan = (await http.get(f"/api/evidence/{done['plan_evidence_id']}")).json()
                assert plan["result_snapshot"]["actions"][0]["action"]["risk_level"] == "L3"
                await handle.terminate("Step 46 测试清理")
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 46 测试清理")


async def test_query_missing_and_invalid(http: httpx.AsyncClient) -> None:
    assert (await http.get(f"/api/chat/{uuid4()}")).status_code == 404
    assert (await http.get("/api/chat/bad")).status_code == 422


@local_temporal
async def test_worker_restart_and_detached_client_keep_accepted_turn(database: Database) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    gateway = ChatGateway(client, database, settings)
    async with create_worker(client, database, settings, max_cached_workflows=0):
        receipt = await gateway.submit(submission())
    # API/SSE 尚未等待回答时停止 Worker；任务仍由 Temporal 保存。
    async with create_worker(client, database, settings, max_cached_workflows=0):
        answer = await asyncio.wait_for(gateway.wait(receipt), 45)
        assert answer.status == "CLOSED" and answer.answer and answer.evidence_ids
        again = await gateway.submit(submission(request_id=receipt.event_id))
        await gateway.wait(again)
    handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
    await handle.result()
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


@local_temporal
async def test_policy_denied_is_auditable_and_has_no_answer_or_execution(
    database: Database,
) -> None:
    settings = runtime_settings(
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "deny-chat-rollback",
                    "reason": "测试拒绝回滚",
                    "action_names": ["rollback_prod"],
                    "risk_levels": ["L3"],
                    "environments": ["test"],
                    "decision": "deny",
                }
            ]
        }
    )
    client = await Client.connect(settings.temporal_config.address)
    gateway = ChatGateway(client, database, settings)
    async with create_worker(client, database, settings):
        receipt = await gateway.submit(submission(mode="task"))
        answer = await asyncio.wait_for(gateway.wait(receipt), 45)
        assert answer.status == "ESCALATED" and answer.answer is None
        async with database.session() as session:
            records = await LedgerService(session).evidence_for_task(UUID(receipt.task_id))
            assert any(e.source_tool == "action_plan" for e in records)
            assert not any(e.source_tool.startswith("executor.") for e in records)


@local_temporal
@pytest.mark.parametrize("failure", ["forged", "budget"])
async def test_rejected_conclusion_never_streams_claims(
    database: Database, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = runtime_settings(AGENT_CONFIG={"max_steps": 4 if failure == "budget" else 20})

    def llm(config: Settings, request: ChatRequest) -> LLMClient:
        if sum(m.role == "tool" for m in request.messages) == 4:
            return FakeLLM([ScriptedChatStep(invalid_conclusion_response)])
        return configured_llm(config, request)

    monkeypatch.setattr("app.agent.activities.configured_llm", llm)
    client = await Client.connect(settings.temporal_config.address)
    gateway = ChatGateway(client, database, settings)
    async with create_worker(client, database, settings):
        receipt = await gateway.submit(submission())
        answer = await asyncio.wait_for(gateway.wait(receipt), 45)
        assert answer.status == "ESCALATED" and answer.answer is None and answer.evidence_ids == ()


@local_temporal
async def test_committed_request_response_loss_does_not_duplicate_task_or_audit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    original = ChatActivities.persist
    count = 0

    @activity.defn(name="chat.persist")
    async def lose_once(self: ChatActivities, value: str) -> EventReceipt:
        nonlocal count
        result = await original(self, value)
        count += 1
        if count == 1:
            raise ApplicationError("提交后丢响应")
        return result

    monkeypatch.setattr(ChatActivities, "persist", lose_once)
    gateway = ChatGateway(client, database, settings)
    async with create_worker(client, database, settings):
        receipt = await gateway.submit(submission())
        answer = await asyncio.wait_for(gateway.wait(receipt), 45)
        assert answer.status == "CLOSED" and count == 2
        async with database.session() as session:
            records = await LedgerService(session).evidence_for_task(UUID(receipt.task_id))
            assert len([e for e in records if e.source_tool == "chat.request"]) == 1
            audits = await LedgerService(session).audits_for_task(UUID(receipt.task_id))
            assert len([a for a in audits if a.operation == "chat.submit"]) == 1
