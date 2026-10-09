"""临时 API/Worker 的真实回环 SSE 演示；全部运维数据为 Fake。"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2 as httpx
from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus

from app.auth.passwords import hash_password
from app.config import Settings
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("演示仅允许本机临时库及 Temporal")
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    start = end - timedelta(hours=1)
    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        TEMPORAL_CONFIG={"address": address, "task_queue": f"chat-demo-{uuid4().hex}"},
    )
    client = await Client.connect(address)
    await DiscoveryActivities(database, settings).refresh(DiscoveryRequest(end.isoformat(), 3600))
    await TimelineActivities(database, settings).collect(
        TimelineRequest("payment-service", start.isoformat(), end.isoformat())
    )
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
    environment = dict(
        os.environ,
        APP_ENV="test",
        API_HOST="127.0.0.1",
        API_PORT=str(port),
        AUTH_CONFIG=json.dumps(auth),
        DATABASE_URL=url.render_as_string(hide_password=False),
        TEMPORAL_CONFIG=settings.temporal_config.model_dump_json(),
        CONNECTOR_MODE="fake",
        LLM_MODE="fake",
    )
    # 外部环境配置不能改变本演示的策略、网关模式或等待时间。
    for name in ("POLICY_CONFIG", "AGENT_CONFIG", "CHAT_STREAM_TIMEOUT_SECONDS"):
        environment.pop(name, None)
    process = subprocess.Popen(
        [sys.executable, "-m", "app.api.main"],
        cwd=Path(__file__).resolve().parents[1] / "backend",
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    task_ids: list[str] = []

    def request(
        message: str, mode: str = "question", previous: str | None = None
    ) -> dict[str, Any]:
        return {
            "request_id": str(uuid4()),
            "service_name": "payment-service",
            "message": message,
            "mode": mode,
            "previous_task_id": previous,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    try:
        async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=130) as http:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("演示 API 提前退出")
                try:
                    if (await http.get("/health")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("演示 API 未就绪")
            assert (await http.post("/api/chat", json=request("支付为什么报错"))).status_code == 401
            signed_in = await http.post(
                "/api/auth/login",
                headers={"X-Ops-Login": "1"},
                json={"username": "local-demo-owner", "password": password},
            )
            assert signed_in.status_code == 200
            http.headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]

            async def send(body: dict[str, Any]) -> dict[str, Any]:
                done: dict[str, Any] | None = None
                event = ""
                print(f"\n本人：{body['message']}", flush=True)
                async with http.stream("POST", "/api/chat", json=body) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    async for line in response.aiter_lines():
                        if line.startswith("event: "):
                            event = line[7:]
                        elif line.startswith("data: "):
                            payload = json.loads(line[6:])
                            if event == "task":
                                task_ids.append(payload["task_id"])
                                print(
                                    "Human Task / Workflow："
                                    + payload["task_id"]
                                    + " / "
                                    + payload["workflow_id"],
                                    flush=True,
                                )
                            elif event == "delta":
                                print(payload["text"], end="", flush=True)
                            elif event == "error":
                                raise RuntimeError(payload["message"])
                            elif event == "done":
                                done = payload
                assert done is not None and done["evidence_ids"]
                print("\n任务状态：" + done["status"], flush=True)
                for reference in done["evidence_ids"]:
                    evidence = (await http.get(f"/api/evidence/{reference}")).json()
                    assert evidence["task_id"] == done["task_id"]
                return done

            async with create_worker(client, database, settings):
                message = "payment-service 为什么出现 5xx？请引用查询证据。"
                if interactive:
                    message = (
                        await asyncio.to_thread(input, "请输入支付场景问题（回车用默认）：")
                    ) or message
                question = request(message)
                first = await send(question)
                assert first["status"] == "CLOSED"
                repeated = await send(question)
                assert repeated["task_id"] == first["task_id"] and repeated == first
                follow = await send(request("哪些证据支持这个判断？", previous=first["task_id"]))
                assert follow["status"] == "CLOSED"
                assert set(follow["evidence_ids"]).isdisjoint(first["evidence_ids"])
                action = await send(
                    request("请调查并回滚 payment-service v2.3.7 到 v2.3.6", "task")
                )
                assert (
                    action["status"] == "WAITING_APPROVAL"
                    and action["policy_decision"] == "need_approval"
                )
                plan = (await http.get(f"/api/evidence/{action['plan_evidence_id']}")).json()[
                    "result_snapshot"
                ]
                assert plan["actions"][0]["action"]["risk_level"] == "L3"
                calls = (await http.get(f"/api/tasks/{action['task_id']}/tool-calls")).json()[
                    "items"
                ]
                assert not any(call["operation"] == "execute_action" for call in calls)
                print("L3 / need_approval；审批前实际运维动作：0；重复提交新增任务：0", flush=True)
                for task_id in (first["task_id"], follow["task_id"]):
                    handle = client.get_workflow_handle_for(
                        AITaskWorkflow.run, f"ai-task-{task_id}"
                    )
                    await handle.result()
                print("Step 46 AI Chat API Fake 演示全部通过", flush=True)
    finally:
        for task_id in set(task_ids):
            handle = client.get_workflow_handle_for(AITaskWorkflow.run, f"ai-task-{task_id}")
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 46 演示清理")
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 10)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        await database.dispose()
