"""本机隔离 PostgreSQL/Temporal：全部查询与人工操作、审计及恢复。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

from app.api.console import get_control_gateway
from app.api.main import create_app
from app.config import Settings
from app.db.session import Database
from app.ledger.models import AuditEventType, AuditRecord
from app.ledger.service import LedgerService
from app.tasks.console_models import ControlCommand, ControlReceipt
from app.tasks.control import ControlActivities, ControlGateway, ControlStore
from app.tasks.control_workflow import TaskControlWorkflow
from app.tasks.models import AITask
from app.tasks.safety.service import require_automation_active
from app.tasks.service import TaskService, TaskStateConflict
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanQuestion, TaskSnapshot, WorkflowInput
from app.triggers.models import OpsEvent
from tests.auth_support import PASSWORD, auth_config
from tests.test_approval_integration import wait_prompt
from tests.test_console import READ_PATHS
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import new_task, runtime_settings
from tests.test_postmortem_integration import learning
from tests.test_postmortem_integration import settings as learning_settings
from tests.test_reviewer_integration import database, migrated_schema

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="运行 check-console.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


@pytest.fixture
def api(database: Database) -> FastAPI:
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=auth_config()))
    app.state.database = database
    return app


@pytest_asyncio.fixture
async def http(api: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/auth/login",
            headers={"X-Ops-Login": "1"},
            json={"username": "local-owner", "password": PASSWORD},
        )
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client


async def test_every_query_success_missing_invalid_and_exact_evidence(
    http: httpx.AsyncClient, database: Database
) -> None:
    from app.learning.service import LearningStore

    request = await learning(database)
    report = await LearningStore(database, learning_settings()).generate(request)
    task_id = request.task.task_id
    async with database.session() as session:
        event = (await session.scalars(select(OpsEvent))).first()
        assert event is not None
        records = await LedgerService(session).evidence_for_task(UUID(task_id))
        calls = [
            a
            for a in await LedgerService(session).audits_for_task(UUID(task_id))
            if a.event_type is AuditEventType.TOOL_CALL
        ]
        history = await TaskService(session).history(UUID(task_id))
    detail_ids = {
        "/api/tasks/{id}": task_id,
        "/api/tasks/{id}/interaction": task_id,
        "/api/tasks/{id}/evidence": task_id,
        "/api/tasks/{id}/tool-calls": task_id,
        "/api/tasks/{id}/status-history": task_id,
        "/api/events/{id}": str(event.id),
        "/api/evidence/{id}": str(records[0].id),
        "/api/tool-calls/{id}": str(calls[0].id),
        "/api/status-history/{id}": str(history[0].id),
        "/api/incidents/{id}": report.evidence_id,
    }
    for path in READ_PATHS:
        target = path.format(id=detail_ids[path]) if "{id}" in path else path
        response = await http.get(target)
        assert response.status_code == 200, (target, response.text)
        if "{id}" in path:
            assert (await http.get(path.format(id=uuid4()))).status_code == 404
            assert (await http.get(path.format(id="bad-id"))).status_code == 422
        else:
            assert (await http.get(path + "?limit=101")).status_code == 422
    assert (await http.get(f"/api/evidence/{records[0].id}")).json()["result_snapshot"] == records[
        0
    ].result_snapshot
    report_json = (await http.get(f"/api/incidents/{report.evidence_id}")).json()
    assert len(report_json["report"]["sections"]) == 13
    assert report_json["evidence_id"] == report.evidence_id
    assert (await http.get(f"/api/incidents/{records[0].id}")).status_code == 404
    assert (await http.get(f"/api/tool-calls/{history[0].id}")).status_code == 404


async def test_query_filters_paging_empty_and_foreign_resource(
    http: httpx.AsyncClient, database: Database
) -> None:
    first, second = await new_task(database), await new_task(database)
    tasks = (await http.get("/api/tasks?source=Alert&status=NEW&limit=1")).json()
    next_page = (await http.get("/api/tasks?source=Alert&status=NEW&limit=1&offset=1")).json()
    assert tasks["total"] >= 2 and next_page["total"] == tasks["total"]
    assert tasks["items"][0]["id"] != next_page["items"][0]["id"]
    assert (await http.get("/api/tasks?status=invalid")).status_code == 422
    assert (await http.get("/api/events?origin=invalid")).status_code == 422
    assert (await http.get("/api/events?service_name=none")).json()["items"] == []
    assert (await http.get(f"/api/evidence?task_id={first.task_id}")).json()["items"] == []
    assert (await http.get(f"/api/tool-calls?task_id={second.task_id}")).json()["items"] == []
    for collection in ("evidence", "tool-calls"):
        assert (await http.get(f"/api/{collection}?task_id={uuid4()}")).status_code == 404
        assert (await http.get(f"/api/{collection}?task_id=bad")).status_code == 422


@local_temporal
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_api_approval_resumes_workflow_and_duplicate_keeps_same_audit(
    http: httpx.AsyncClient, database: Database, decision: str, api: FastAPI
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    api.dependency_overrides[get_control_gateway] = lambda: ControlGateway(
        client, settings, database
    )
    task = await new_task(database)
    handle = None
    try:
        async with create_worker(client, database, settings):
            handle = await start_task_workflow(
                client,
                WorkflowInput(task.task_id, investigation_json=SPEC.model_dump_json()),
                task_queue=settings.temporal_config.task_queue,
            )
            prompt = await wait_prompt(handle)
            interaction = (await http.get(f"/api/tasks/{task.task_id}/interaction")).json()
            assert interaction["approval"]["action_hash"] == prompt.action_hash
            body = {
                "approval_id": prompt.approval_id,
                "wait_version": prompt.task.version,
                "action_hash": prompt.action_hash,
                "decision": decision,
            }
            assert (
                await http.post(
                    f"/api/tasks/{task.task_id}/approval", json={**body, "action_hash": "0" * 64}
                )
            ).status_code == 409
            response = await http.post(f"/api/tasks/{task.task_id}/approval", json=body)
            assert response.status_code == 202, response.text
            result = await asyncio.wait_for(handle.result(), 30)
            assert result.task and result.task.status is (
                TaskStatus.EXECUTING if decision == "approved" else TaskStatus.ESCALATED
            )
            again = await http.post(f"/api/tasks/{task.task_id}/approval", json=body)
            assert again.json() == response.json()
            opposite = "rejected" if decision == "approved" else "approved"
            assert (
                await http.post(
                    f"/api/tasks/{task.task_id}/approval", json={**body, "decision": opposite}
                )
            ).status_code == 409
        async with database.session() as session:
            audits = [
                a
                for a in await LedgerService(session).audits_for_task(UUID(task.task_id))
                if a.operation == "approval.decide"
            ]
            assert (
                len(audits) == 1
                and audits[0].actor == "local-owner"
                and audits[0].occurred_at.tzinfo is UTC
            )
        operation = client.get_workflow_handle(response.json()["operation_id"])
        await Replayer(workflows=[TaskControlWorkflow]).replay_workflow(
            await operation.fetch_history()
        )
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 44 测试清理")


@local_temporal
@pytest.mark.parametrize(
    "kind,status",
    [("judgment", TaskStatus.NEED_HUMAN_JUDGMENT), ("information", TaskStatus.WAITING_INFORMATION)],
)
async def test_api_answer_resumes_and_records_draft(
    http: httpx.AsyncClient, database: Database, kind: str, status: TaskStatus, api: FastAPI
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    api.dependency_overrides[get_control_gateway] = lambda: ControlGateway(
        client, settings, database
    )
    async with database.session() as session, session.begin():
        human_task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="问答 API 验收", reason="非关键 Human 任务，无生产动作"
        )
        task = TaskSnapshot(str(human_task.id), human_task.status, human_task.status_version)
    handle = None
    try:
        async with create_worker(client, database, settings):
            handle = await start_task_workflow(
                client,
                WorkflowInput(
                    task.task_id,
                    human_questions=[HumanQuestion(status, "请提供实际业务判断或信息")],
                    postmortem_enabled=False,
                ),
                task_queue=settings.temporal_config.task_queue,
            )
            async with asyncio.timeout(30):
                while True:
                    progress = await handle.query(AITaskWorkflow.progress)
                    if progress.human_prompt:
                        prompt = progress.human_prompt
                        break
                    await asyncio.sleep(0.03)
            assert (await http.get(f"/api/tasks/{task.task_id}/interaction")).json()["question"][
                "question_id"
            ] == prompt.question_id
            body = {
                "question_id": prompt.question_id,
                "wait_version": prompt.task.version,
                "answer": "先保障支付业务，现实信息已确认",
            }
            wrong_kind = "information" if kind == "judgment" else "judgment"
            assert (
                await http.post(f"/api/tasks/{task.task_id}/{wrong_kind}", json=body)
            ).status_code == 409
            response = await http.post(f"/api/tasks/{task.task_id}/{kind}", json=body)
            assert response.status_code == 202, response.text
            result = await asyncio.wait_for(handle.result(), 30)
            assert result.task and result.task.status is TaskStatus.CLOSED
            assert (
                await http.post(f"/api/tasks/{task.task_id}/{kind}", json=body)
            ).json() == response.json()
        async with database.session() as session:
            evidence = await LedgerService(session).get_evidence(
                UUID(response.json()["evidence_id"])
            )
            assert evidence.source_tool == "human.answer"
            audits = await LedgerService(session).audits_for_task(UUID(task.task_id))
            assert any(a.operation == "human.answer" and a.actor == "local-owner" for a in audits)
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 44 测试清理")


@local_temporal
async def test_api_takeover_stops_workflow_and_blocks_old_authorization(
    http: httpx.AsyncClient, database: Database, api: FastAPI
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    api.dependency_overrides[get_control_gateway] = lambda: ControlGateway(
        client, settings, database
    )
    task = await new_task(database)
    handle = None
    try:
        async with create_worker(client, database, settings):
            handle = await start_task_workflow(
                client,
                WorkflowInput(task.task_id, investigation_json=SPEC.model_dump_json()),
                task_queue=settings.temporal_config.task_queue,
            )
            prompt = await wait_prompt(handle)
            body = {"expected_version": prompt.task.version, "reason": "本次由我接管"}
            assert (
                await http.post(
                    f"/api/tasks/{task.task_id}/takeover", json={**body, "expected_version": 0}
                )
            ).status_code == 409
            response = await http.post(f"/api/tasks/{task.task_id}/takeover", json=body)
            assert response.status_code == 202, response.text
            assert (
                await http.post(f"/api/tasks/{task.task_id}/takeover", json=body)
            ).json() == response.json()
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 30)
            assert (await handle.describe()).status is WorkflowExecutionStatus.CANCELED
        async with database.session() as session, session.begin():
            record = await session.get(AITask, UUID(task.task_id))
            assert record and record.status is TaskStatus.ESCALATED
            with pytest.raises(PermissionError, match="接管"):
                await require_automation_active(session, record.id)
            with pytest.raises(TaskStateConflict, match="接管"):
                await TaskService(session).transition(
                    record.id,
                    TaskStatus.INVESTIGATING,
                    expected_status=record.status,
                    expected_version=record.status_version,
                    reason="尝试恢复",
                )
            audits = await LedgerService(session).audits_for_task(record.id)
            assert len([a for a in audits if a.operation == "human.takeover"]) == 1
            assert any(a.operation == "human.takeover" and a.actor == "local-owner" for a in audits)
        from app.tasks.approval.service import ApprovalStore
        from app.tasks.console_queries import ConsoleQueries

        async with database.session() as session:
            ticket, _ = await ConsoleQueries(session).approval(
                UUID(task.task_id), prompt.task.version
            )
        assert not await ApprovalStore(database, settings).is_approved(prompt, ticket.plan)
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 44 测试清理")


async def test_takeover_atomic_on_audit_failure_and_concurrent_retry(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await new_task(database)
    command = ControlCommand(
        task_id=UUID(task.task_id),
        kind="takeover",
        actor="local-owner",
        payload={"expected_version": 0, "reason": "接管"},
    )
    store = ControlStore(database, Settings(APP_ENV="test"))
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> AuditRecord:
        raise RuntimeError("模拟审计失败")

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError):
        await store.record(command)
    monkeypatch.setattr(LedgerService, "append_audit", original)
    async with database.session() as session:
        current = await session.get(AITask, UUID(task.task_id))
        assert current and current.status is TaskStatus.NEW
        assert await LedgerService(session).evidence_for_task(current.id) == []
    results = await asyncio.gather(store.record(command), store.record(command))
    assert results[0] == results[1]


@local_temporal
@pytest.mark.parametrize("kind", ["approval", "judgment", "information", "takeover"])
async def test_every_write_missing_task_and_invalid_body(
    kind: str, http: httpx.AsyncClient, database: Database, api: FastAPI
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    api.dependency_overrides[get_control_gateway] = lambda: ControlGateway(
        client, settings, database
    )
    payloads = {
        "approval": {
            "approval_id": str(uuid4()),
            "wait_version": 1,
            "action_hash": "0" * 64,
            "decision": "approved",
        },
        "judgment": {"question_id": str(uuid4()), "wait_version": 1, "answer": "业务判断"},
        "information": {"question_id": str(uuid4()), "wait_version": 1, "answer": "补充信息"},
        "takeover": {"expected_version": 0, "reason": "接管"},
    }
    path = f"/api/tasks/{uuid4()}/{kind}"
    assert (await http.post(path, json=payloads[kind])).status_code == 404
    assert (await http.post(path, json={})).status_code == 422
    assert (await http.post(path, json={**payloads[kind], "actor": "forged"})).status_code == 422
    if kind != "takeover":
        task = await new_task(database)
        async with create_worker(client, database, settings):
            assert (
                await http.post(f"/api/tasks/{task.task_id}/{kind}", json=payloads[kind])
            ).status_code == 404


@local_temporal
async def test_control_commit_and_delivery_response_loss_survive_worker_restart(
    http: httpx.AsyncClient, database: Database, api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    api.dependency_overrides[get_control_gateway] = lambda: ControlGateway(
        client, settings, database
    )
    original_record, original_deliver = ControlStore.record, ControlActivities.deliver
    counts = {"record": 0, "deliver": 0}
    delivered = asyncio.Event()

    async def lose_record(
        self: ControlStore, command: ControlCommand, *, allow_recovery: bool = False
    ) -> ControlReceipt:
        result = await original_record(self, command, allow_recovery=allow_recovery)
        counts["record"] += 1
        if counts["record"] == 1:
            raise RuntimeError("模拟记录提交后丢响应")
        return result

    @activity.defn(name="task.control.deliver")
    async def lose_delivery(self: ControlActivities, command_json: str, receipt_json: str) -> None:
        await original_deliver(self, command_json, receipt_json)
        counts["deliver"] += 1
        if counts["deliver"] == 1:
            delivered.set()
            raise RuntimeError("模拟信号提交后丢响应")

    monkeypatch.setattr(ControlStore, "record", lose_record)
    monkeypatch.setattr(ControlActivities, "deliver", lose_delivery)
    task = await new_task(database)
    handle = None
    pending = None
    try:
        async with create_worker(client, database, settings, max_cached_workflows=0):
            handle = await start_task_workflow(
                client,
                WorkflowInput(task.task_id, investigation_json=SPEC.model_dump_json()),
                task_queue=settings.temporal_config.task_queue,
            )
            prompt = await wait_prompt(handle)
            pending = asyncio.create_task(
                http.post(
                    f"/api/tasks/{task.task_id}/approval",
                    json={
                        "approval_id": prompt.approval_id,
                        "wait_version": prompt.task.version,
                        "action_hash": prompt.action_hash,
                        "decision": "approved",
                    },
                )
            )
            await asyncio.wait_for(delivered.wait(), 30)
        async with create_worker(client, database, settings, max_cached_workflows=0):
            response = await asyncio.wait_for(pending, 30)
            assert response.status_code == 202, response.text
            result = await asyncio.wait_for(handle.result(), 30)
            assert result.task and result.task.status is TaskStatus.EXECUTING
        assert counts == {"record": 2, "deliver": 2}
        async with database.session() as session:
            audits = await LedgerService(session).audits_for_task(UUID(task.task_id))
            assert len([a for a in audits if a.operation == "approval.decide"]) == 1
        control = client.get_workflow_handle(response.json()["operation_id"])
        await Replayer(workflows=[TaskControlWorkflow]).replay_workflow(
            await control.fetch_history()
        )
    finally:
        if pending and not pending.done():
            pending.cancel()
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 44 测试清理")


@local_temporal
async def test_failed_control_workflow_can_retry_same_request_without_duplicate_authorization(
    http: httpx.AsyncClient, database: Database, api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    api.dependency_overrides[get_control_gateway] = lambda: ControlGateway(
        client, settings, database
    )
    original = ControlStore.record
    calls = 0

    async def lose_once(
        self: ControlStore, command: ControlCommand, *, allow_recovery: bool = False
    ) -> ControlReceipt:
        nonlocal calls
        result = await original(self, command, allow_recovery=allow_recovery)
        calls += 1
        if calls == 1:
            raise ApplicationError("模拟提交后控制流程失败", non_retryable=True)
        return result

    monkeypatch.setattr(ControlStore, "record", lose_once)
    task = await new_task(database)
    handle = None
    try:
        async with create_worker(client, database, settings):
            handle = await start_task_workflow(
                client,
                WorkflowInput(task.task_id, investigation_json=SPEC.model_dump_json()),
                task_queue=settings.temporal_config.task_queue,
            )
            prompt = await wait_prompt(handle)
            body = {
                "approval_id": prompt.approval_id,
                "wait_version": prompt.task.version,
                "action_hash": prompt.action_hash,
                "decision": "approved",
            }
            path = f"/api/tasks/{task.task_id}/approval"
            assert (await http.post(path, json=body)).status_code == 503
            assert (await http.post(path, json=body)).status_code == 202
            result = await asyncio.wait_for(handle.result(), 30)
            assert result.task and result.task.status is TaskStatus.EXECUTING
        async with database.session() as session:
            audits = await LedgerService(session).audits_for_task(UUID(task.task_id))
            assert len([a for a in audits if a.operation == "approval.decide"]) == 1
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 44 测试清理")


@local_temporal
@pytest.mark.parametrize(
    "kind,status",
    [("judgment", TaskStatus.NEED_HUMAN_JUDGMENT), ("information", TaskStatus.WAITING_INFORMATION)],
)
async def test_legacy_recovery_signal_is_available_via_api_with_answer_evidence(
    kind: str, status: TaskStatus, http: httpx.AsyncClient, database: Database, api: FastAPI
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    api.dependency_overrides[get_control_gateway] = lambda: ControlGateway(
        client, settings, database
    )
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="旧等待恢复 API 验收", reason="验证既有恢复信号"
        )
        task_id = str(task.id)
    handle = None
    try:
        async with create_worker(client, database, settings):
            handle = await start_task_workflow(
                client,
                WorkflowInput(task_id, waits=[status], postmortem_enabled=False),
                task_queue=settings.temporal_config.task_queue,
            )
            async with asyncio.timeout(30):
                while True:
                    progress = await handle.query(AITaskWorkflow.progress)
                    if progress.legacy_wait:
                        break
                    await asyncio.sleep(0.03)
            pending = (await http.get(f"/api/tasks/{task_id}/interaction")).json()
            assert pending["question"] is None and pending["recovery"] is not None
            body = {
                "question_id": pending["recovery_question_id"],
                "wait_version": pending["status_version"],
                "answer": "数据已恢复，请重新采集核验",
            }
            response = await http.post(f"/api/tasks/{task_id}/{kind}", json=body)
            assert response.status_code == 202, response.text
            result = await asyncio.wait_for(handle.result(), 30)
            assert result.task and result.task.status is TaskStatus.CLOSED
            assert (
                await http.post(f"/api/tasks/{task_id}/{kind}", json=body)
            ).json() == response.json()
        async with database.session() as session:
            evidence = await LedgerService(session).get_evidence(
                UUID(response.json()["evidence_id"])
            )
            assert evidence.source_tool == "human.answer" and evidence.task_id == UUID(task_id)
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 44 测试清理")
