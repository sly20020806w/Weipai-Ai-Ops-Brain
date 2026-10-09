"""隔离库与 Worker 上的真实回环 HTTP 演示，可手动批准 Fake 回滚。"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from datetime import timedelta
from functools import partial
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from uuid import uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError, WorkflowHandle

from app.auth.passwords import hash_password
from app.config import Settings
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector
from app.connectors.observability.fake import SAMPLE_END
from app.db.base import utc_now
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.learning.demo import seed_incident
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanQuestion, WorkflowInput, WorkflowProgress
from app.tools.verification_runtime import fake_verification_registry
from app.triggers.schemas import NormalizedEvent
from app.triggers.service import EventService
from app.verifier.activities import VerifierActivities
from app.verifier.models import ResourceExpectation


async def wait_prompt(
    handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress], *, human: bool = False
) -> WorkflowProgress:
    async with asyncio.timeout(45):
        while True:
            progress = await handle.query(AITaskWorkflow.progress)
            if progress.human_prompt if human else progress.approval_prompt:
                return progress
            if progress.task and progress.task.status is TaskStatus.ESCALATED:
                raise RuntimeError("演示任务提前转人工")
            await asyncio.sleep(0.05)


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("API 演示只允许本机临时库和 Temporal")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    password = secrets.token_urlsafe(24)
    auth = {
        "username": "local-demo-owner",
        "password_hash": hash_password(password),
        "session_secret": secrets.token_hex(32),
        "public_origin": origin,
    }
    settings = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        VERIFICATION_CONFIG={
            "resources_by_service": {
                "payment-service": (
                    ResourceExpectation(
                        product="rds",
                        region_id="cn-hangzhou",
                        resource_id="rm-payment",
                        healthy_status="Running",
                    ),
                )
            }
        },
        TEMPORAL_CONFIG={"address": address, "task_queue": f"console-demo-{uuid4().hex}"},
    )
    database = Database(url)
    client = await Client.connect(address)
    connector = FakeKubernetesWriteConnector(clock=lambda: SAMPLE_END)
    executor = ExecutorActivities(database, settings, connector=connector)
    verifier = VerifierActivities(
        database,
        settings,
        registry_factory=partial(
            fake_verification_registry,
            window_start=SAMPLE_END,
            window_end=SAMPLE_END + timedelta(minutes=5),
        ),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "app.api.main"],
        cwd=Path(__file__).resolve().parents[1] / "backend",
        env=dict(
            os.environ,
            APP_ENV="test",
            API_HOST="127.0.0.1",
            API_PORT=str(port),
            AUTH_CONFIG=json.dumps(auth),
            TEMPORAL_CONFIG=settings.temporal_config.model_dump_json(),
            DATABASE_URL=url.render_as_string(hide_password=False),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    handles: list[WorkflowHandle[Any, Any]] = []

    def request(
        path: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        call = Request(
            origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with opener.open(call, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except HTTPError as error:
            return error.code, json.loads(error.read())

    async def call(
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        expected: int = 200,
    ) -> Any:
        status, data = await asyncio.to_thread(request, path, body, headers)
        assert status == expected, (path, status, data)
        return data

    try:
        for _ in range(150):
            if process.poll() is not None:
                raise RuntimeError("演示 API 提前退出")
            try:
                if (await asyncio.to_thread(request, "/health"))[0] == 200:
                    break
            except OSError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("演示 API 未就绪")
        await call("/api/tasks", expected=401)
        login = await call(
            "/api/auth/login",
            {"username": auth["username"], "password": password},
            {"X-Ops-Login": "1"},
        )
        csrf = {"X-CSRF-Token": login["csrf_token"]}
        async with create_worker(
            client, database, settings, executor_activities=executor, verifier_activities=verifier
        ):
            snapshot, spec = await seed_incident(database, settings)
            handle = await start_task_workflow(
                client,
                WorkflowInput(
                    snapshot.task_id,
                    investigation_json=spec.model_dump_json(),
                    execution_enabled=True,
                ),
                task_queue=settings.temporal_config.task_queue,
            )
            handles.append(handle)
            await wait_prompt(handle)
            pending = await call(f"/api/tasks/{snapshot.task_id}/interaction")
            ticket = pending["approval"]
            print(f"本机 API：{origin}；任务：{snapshot.task_id}", flush=True)
            print("待审批：payment-service v2.3.7 → v2.3.6，L3；全部为 Fake。", flush=True)
            decision = "approved"
            if interactive:
                text = await asyncio.to_thread(input, "输入 approve 批准，或 reject 拒绝：")
                if text.strip() not in {"approve", "reject"}:
                    raise ValueError("只接受 approve/reject")
                decision = "approved" if text.strip() == "approve" else "rejected"
            body = {
                "approval_id": ticket["approval_id"],
                "wait_version": ticket["wait_version"],
                "action_hash": ticket["action_hash"],
                "decision": decision,
            }
            receipt = await call(
                f"/api/tasks/{snapshot.task_id}/approval", body, csrf, expected=202
            )
            progress = await asyncio.wait_for(handle.result(), 60)
            expected_status = TaskStatus.CLOSED if decision == "approved" else TaskStatus.ESCALATED
            assert progress.task and progress.task.status is expected_status
            repeated = await call(
                f"/api/tasks/{snapshot.task_id}/approval", body, csrf, expected=202
            )
            assert repeated == receipt
            assert connector.execution_count == (1 if decision == "approved" else 0)
            print(
                f"Fake 回滚次数：{connector.execution_count}；重复审批未增加执行次数。", flush=True
            )
            for collection in ("evidence", "tool-calls", "status-history"):
                await call(f"/api/tasks/{snapshot.task_id}/{collection}")
            await call(f"/api/evidence/{receipt['evidence_id']}")
            if decision == "approved":
                report = await call(f"/api/incidents/{progress.postmortem_evidence_id}")
                assert len(report["report"]["sections"]) == 13
                for task_id in report["report"]["improvement_task_ids"]:
                    handles.append(client.get_workflow_handle(f"ai-task-{task_id}"))
            print(
                f"API 审批 202 → {expected_status.value}；Evidence：{receipt['evidence_id']}；"
                "重复提交同一回执。",
                flush=True,
            )
            for kind, status in (
                ("judgment", TaskStatus.NEED_HUMAN_JUDGMENT),
                ("information", TaskStatus.WAITING_INFORMATION),
                ("takeover", TaskStatus.NEED_HUMAN_JUDGMENT),
            ):
                async with database.session() as session, session.begin():
                    event = (
                        await EventService(session).accept(
                            [
                                NormalizedEvent(
                                    origin="manual",
                                    source=TaskSource.HUMAN,
                                    external_id=f"console-{uuid4().hex}",
                                    service_name="payment-service",
                                    title="人工操作 API 演示",
                                    occurred_at=utc_now(),
                                )
                            ]
                        )
                    )[0]
                question_handle = await start_task_workflow(
                    client,
                    WorkflowInput(
                        event.task_id,
                        human_questions=[HumanQuestion(status, "请确认业务优先级或补充信息")],
                        postmortem_enabled=False,
                    ),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(question_handle)
                await wait_prompt(question_handle, human=True)
                pending = await call(f"/api/tasks/{event.task_id}/interaction")
                question = pending["question"]
                if kind == "takeover":
                    await call(
                        f"/api/tasks/{event.task_id}/takeover",
                        {
                            "expected_version": pending["status_version"],
                            "reason": "由我接管，停止自动化",
                        },
                        csrf,
                        expected=202,
                    )
                    try:
                        await asyncio.wait_for(question_handle.result(), 30)
                    except WorkflowFailureError:
                        pass
                    assert (
                        await question_handle.describe()
                    ).status is WorkflowExecutionStatus.CANCELED
                    assert (await call(f"/api/tasks/{event.task_id}"))["status"] == "ESCALATED"
                    print(
                        "API 人工接管 202 → ESCALATED，Workflow 已取消，后续自动化被禁止。",
                        flush=True,
                    )
                else:
                    await call(
                        f"/api/tasks/{event.task_id}/{kind}",
                        {
                            "question_id": question["question_id"],
                            "wait_version": question["task"]["version"],
                            "answer": "先保障支付业务，实际情况已确认",
                        },
                        csrf,
                        expected=202,
                    )
                    result = await asyncio.wait_for(question_handle.result(), 45)
                    assert result.task and result.task.status is TaskStatus.CLOSED
                    print(
                        f"API {status.value} 回答 202 → CLOSED，回答证据与 Knowledge 草稿已保存。",
                        flush=True,
                    )
            for collection in ("tasks", "events", "evidence", "tool-calls", "incidents"):
                await call(f"/api/{collection}")
        print("Step 44 任务闭环 API Fake 演示全部通过；真实运维请求 0。", flush=True)
    finally:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 10)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 44 演示清理")
        await connector.aclose()
        await database.dispose()
