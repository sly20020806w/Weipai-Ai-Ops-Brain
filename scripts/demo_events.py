"""启动临时本机 HTTP 服务，以实际 POST 演示 Step 21，结束后清理。"""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from uuid import UUID, uuid4

import httpx2 as httpx
from pydantic import SecretStr
from sqlalchemy import func, select
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.config import Settings, parse_database_url
from app.connectors.kubernetes.config import KubernetesConfig
from app.db.session import Database
from app.tasks.config import TemporalConfig
from app.tasks.models import AITask
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, validate_placeholder_settings
from app.tasks.workflow import AITaskWorkflow
from app.triggers.config import TriggerConfig
from app.triggers.models import OpsEvent
from app.triggers.schemas import WatchInput
from app.triggers.workflow import KubernetesEventWatchWorkflow


async def run_demo() -> None:
    url = parse_database_url(os.environ["TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or not (url.database or "").startswith("weipai_db_test_"):
        raise ValueError("演示只允许独立的本机临时数据库")
    temporal = TemporalConfig(
        address=os.environ["TEST_TEMPORAL_ADDRESS"], task_queue=f"events-demo-{uuid4().hex}"
    )
    # 隔离宿主配置；签名密钥随机生成，只在父子进程环境/内存中存在。
    for field in Settings.model_fields.values():
        if isinstance(field.validation_alias, str):
            os.environ.pop(field.validation_alias, None)
    key = secrets.token_urlsafe(32)
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=url.render_as_string(hide_password=False),
        TEMPORAL_CONFIG=temporal,
        TRIGGER_CONFIG=TriggerConfig(
            webhook_secrets={"prometheus": SecretStr(key)}, response_timeout_seconds=30
        ),
        KUBERNETES_CONFIG=KubernetesConfig(
            cluster_name=f"demo-{uuid4().hex}", base_url="https://fake.example.invalid"
        ),
    )
    validate_placeholder_settings(settings)
    database = Database(url)
    client = await Client.connect(temporal.address, namespace=temporal.namespace)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    environment = dict(
        os.environ,
        APP_ENV="test",
        CONNECTOR_MODE="fake",
        LLM_MODE="fake",
        API_HOST="127.0.0.1",
        API_PORT=str(port),
        DATABASE_URL=url.render_as_string(hide_password=False),
        TEMPORAL_CONFIG=temporal.model_dump_json(),
        TRIGGER_CONFIG=json.dumps(
            {"webhook_secrets": {"prometheus": key}, "response_timeout_seconds": 30}
        ),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "app.api.main"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    cleanup: set[str] = set()
    try:
        async with (
            create_worker(client, database, settings),
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=40, trust_env=False
            ) as http,
        ):
            async with asyncio.timeout(20):
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("临时 API 未能启动")
                    try:
                        if (await http.get("/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
            print(f"临时 API 已启动：http://127.0.0.1:{port}；Worker 使用专用队列", flush=True)
            body = json.dumps(
                {
                    "version": "4",
                    "alerts": [
                        {
                            "status": "firing",
                            "labels": {"alertname": "Payment5xxHigh", "service": "payment-service"},
                            "startsAt": "2026-10-01T01:30:00Z",
                        }
                    ],
                }
            ).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(
                key.encode(), timestamp.encode() + b"." + body, hashlib.sha256
            ).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-Ops-Timestamp": timestamp,
                "X-Ops-Signature": f"sha256={signature}",
            }
            first = await http.post("/webhooks/prometheus", content=body, headers=headers)
            first.raise_for_status()
            assert first.status_code == 202
            receipt = first.json()["events"][0]
            cleanup.add(receipt["workflow_id"])
            handle = client.get_workflow_handle(receipt["workflow_id"])
            async with asyncio.timeout(20):
                while True:
                    progress = await handle.query(AITaskWorkflow.progress)
                    if (
                        progress.task is not None
                        and progress.task.status is TaskStatus.WAITING_INFORMATION
                    ):
                        break
                    await asyncio.sleep(0.1)
            assert (await handle.describe()).status is WorkflowExecutionStatus.RUNNING
            second = await http.post("/webhooks/prometheus", content=body, headers=headers)
            second.raise_for_status()
            duplicate = second.json()["events"][0]
            assert duplicate["duplicate"] and duplicate["task_id"] == receipt["task_id"]
            bad_headers = headers | {"X-Ops-Signature": "sha256=" + "0" * 64}
            invalid = await http.post("/webhooks/prometheus", content=body, headers=bad_headers)
            assert invalid.status_code == 401
            async with database.session() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(OpsEvent)
                        .where(OpsEvent.origin == "prometheus")
                    )
                    == 1
                )
                task = await session.get(AITask, UUID(receipt["task_id"]))
                assert task is not None and task.source is TaskSource.ALERT
            print(
                f"POST 告警：HTTP 202；OpsEvent=1，Alert 任务=1；状态={progress.task.status}",
                flush=True,
            )
            print(f"Workflow ID：{receipt['workflow_id']}", flush=True)
            print(
                "相同告警重投：HTTP 202，duplicate=true，任务 ID 不变；错误签名：HTTP 401",
                flush=True,
            )
            watch = await client.start_workflow(
                KubernetesEventWatchWorkflow.run,
                WatchInput("payment"),
                id=f"events-demo-watch-{uuid4()}",
                task_queue=temporal.task_queue,
            )
            cleanup.add(watch.id)
            initial_run_id = watch.first_execution_run_id
            assert initial_run_id is not None
            async with asyncio.timeout(30):
                while True:
                    async with database.session() as session:
                        event = await session.scalar(
                            select(OpsEvent).where(OpsEvent.origin == "kubernetes")
                        )
                        if event is not None:
                            cleanup.add(f"ai-task-{event.task_id}")
                            assert event.service_name == "payment-service"
                            break
                    await asyncio.sleep(0.1)
            # 事件提交早于任务派发；等待子 Workflow 完成，才认为演示成功。
            async with asyncio.timeout(30):
                while (await watch.describe()).run_id == initial_run_id:
                    await asyncio.sleep(0.1)
            assert event is not None
            assert (
                await client.get_workflow_handle(f"ai-task-{event.task_id}").describe()
            ).status is WorkflowExecutionStatus.RUNNING
            print(
                "Fake K8s Warning：已生成 OpsEvent，关联 payment-service；演示全部通过", flush=True
            )
            print(
                "Temporal UI 可按上述 Workflow ID 查看历史；演示结束后执行会显示 Terminated",
                flush=True,
            )
    finally:
        try:
            # 也清理失败请求遗留的接入 Workflow；仅限本次专用队列。
            async for execution in client.list_workflows(
                f'TaskQueue = "{temporal.task_queue}" AND ExecutionStatus = "Running"'
            ):
                cleanup.add(execution.id)
            for workflow_id in cleanup:
                try:
                    await client.get_workflow_handle(workflow_id).terminate("Step 21 演示清理")
                except RPCError as error:
                    if error.status != RPCStatusCode.NOT_FOUND:
                        raise
        finally:
            if sys.platform == "win32" and process.poll() is None:
                # Windows venv 启动器会再创建 Python 子进程，须停止本次 PID 的整棵树。
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            await database.dispose()
            print("临时 API、Worker、演示 Workflow 已停止", flush=True)


if __name__ == "__main__":
    asyncio.run(run_demo())
