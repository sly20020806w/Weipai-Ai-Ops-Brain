"""真实回环 HTTP 运行演示：Fake 图/巡检、本人目录编辑与只追加审计。"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from sqlalchemy.engine import URL
from temporalio.client import Client

from app.auth.passwords import hash_password
from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.inspection.fake import sample_facts
from app.db.base import utc_now
from app.db.session import Database
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import RunbookContent
from app.tasks.inspection.demo import case


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("认知 API 演示只允许本机临时库与 Temporal")
    database = Database(url)
    settings = Settings(APP_ENV="test", CONNECTOR_MODE="fake", LLM_MODE="fake")
    process = None
    try:
        await DiscoveryActivities(database, settings).refresh(
            DiscoveryRequest(utc_now().isoformat(), 3600)
        )
        client = await Client.connect(
            address, namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default")
        )
        report = await case(
            database, client, sample_facts("payment-service"), FakeFeishuConnector()
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
        process = subprocess.Popen(
            [sys.executable, "-m", "app.api.main"],
            cwd=Path(__file__).resolve().parents[1] / "backend",
            env=dict(
                os.environ,
                APP_ENV="test",
                CONNECTOR_MODE="fake",
                LLM_MODE="fake",
                API_HOST="127.0.0.1",
                API_PORT=str(port),
                AUTH_CONFIG=json.dumps(auth),
                DATABASE_URL=url.render_as_string(hide_password=False),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

        def request(
            method: str,
            path: str,
            body: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
        ) -> tuple[int, Any]:
            call = Request(
                origin + path,
                method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            try:
                with opener.open(call, timeout=30) as response:
                    raw = response.read()
                    return response.status, json.loads(raw) if raw else None
            except HTTPError as error:
                return error.code, json.loads(error.read())

        async def call(
            method: str,
            path: str,
            body: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            *,
            expected: int = 200,
        ) -> Any:
            status, data = await asyncio.to_thread(request, method, path, body, headers)
            assert status == expected, (path, status, data)
            return data

        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("演示 API 提前退出")
            try:
                if (await asyncio.to_thread(request, "GET", "/health"))[0] == 200:
                    break
            except OSError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("演示 API 未就绪")
        await call("GET", "/api/services", expected=401)
        login = await call(
            "POST",
            "/api/auth/login",
            {"username": auth["username"], "password": password},
            {"X-Ops-Login": "1"},
        )
        csrf = {"X-CSRF-Token": login["csrf_token"]}
        graph = await call("GET", "/api/services/payment-service")
        assert graph["edges"] and all("freshness_seconds" in edge for edge in graph["edges"])
        risks = await call("GET", "/api/risks?service_name=payment-service&active=true")
        assert risks["total"] == 4
        detail = await call("GET", f"/api/inspections/{report.task_id}")
        report_evidence = next(
            e for e in detail["evidence"] if e["source_tool"] == "inspection.report"
        )
        assert report_evidence["result_snapshot"] == report.model_dump(mode="json")
        print(
            f"真实 HTTP：服务图 {len(graph['nodes'])} 节点/{len(graph['edges'])} 关系；"
            f"巡检风险 4 条；任务 {report.task_id}",
            flush=True,
        )
        print(f"巡检报告 Evidence：{report_evidence['id']}，可按 ID 原样读回。", flush=True)
        content = "支付核心链路优先保障，回滚需独立验证。"
        if interactive:
            content = (
                await asyncio.to_thread(input, "输入一条业务规则（直接回车使用样例）：") or content
            )
        rule = {"kind": "business_rule", "content": content, "source": "本人本机验收输入"}
        await call("POST", "/api/knowledge", rule, expected=403)
        knowledge = await call("POST", "/api/knowledge", rule, csrf, expected=201)
        assert (await call("GET", f"/api/knowledge/{knowledge['id']}"))["content"] == content
        await call(
            "PUT",
            f"/api/knowledge/{knowledge['id']}",
            {**rule, "content": content + "（已更新）"},
            csrf,
        )
        guide = payment_runbook("payment-api-demo").model_dump(
            mode="json", include=set(RunbookContent.model_fields)
        )
        runbook = await call("POST", "/api/runbooks", guide, csrf, expected=201)
        changed = await call(
            "PUT",
            f"/api/runbooks/{runbook['id']}",
            {**guide, "description": "更新后的支付检查"},
            csrf,
        )
        assert changed["content_version"] == 2 and changed["maturity"] == "draft"
        for name, identity in (("knowledge", knowledge["id"]), ("runbooks", runbook["id"])):
            await call("DELETE", f"/api/{name}/{identity}", headers=csrf, expected=204)
            await call("GET", f"/api/{name}/{identity}", expected=404)
        audits = await call("GET", "/api/audits?actor=local-demo-owner&event_type=catalog_edit")
        assert audits["total"] == 6
        metrics = await call("GET", "/api/metrics")
        assert len(metrics["metrics"]) == 10
        for center in (
            "releases",
            "tickets",
            "war-rooms",
            "architecture-reviews",
            "automations",
            "changes",
        ):
            await call("GET", f"/api/{center}")
        schema = await call("GET", "/openapi.json")
        assert "/api/war-rooms/{task_id}" in schema["paths"]
        print(
            "真实 HTTP：Knowledge/Runbook 新建 201 → 更新 200 → 删除 204 → 读回 404；"
            "本人编辑审计 6 条；能力指标 10 项。",
            flush=True,
        )
        print("Step 45 演示通过；所有运维数据为本机 Fake，未执行运维写动作。", flush=True)
    finally:
        if process is not None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        await database.dispose()
