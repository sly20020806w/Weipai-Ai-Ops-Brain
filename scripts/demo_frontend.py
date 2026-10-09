"""隔离本机 PostgreSQL/API/前端的真实浏览器外壳验收。"""

import asyncio
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener
from uuid import UUID

from approval_pages_scenario import ApprovalPageScenario, approval_scenario
from chat_pages_scenario import chat_scenario
from sqlalchemy import select
from sqlalchemy.engine import URL

from app.auth.passwords import hash_password
from app.config import Settings
from app.db.session import Database
from app.knowledge.models import KnowledgeEntry
from app.learning.demo import run_demo as incident_demo
from app.ledger.models import CatalogAudit
from app.runbooks.models import Runbook
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import RunbookContent
from app.tasks.catalog_service import CatalogService
from app.tasks.console_queries import ConsoleQueries
from app.tasks.operations_models import KnowledgeInput, RunbookInput
from app.tasks.operations_queries import OperationsQueries
from app.tasks.states import TaskSource


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def ready(process: subprocess.Popen[bytes], url: str) -> None:
    def probe() -> bool:
        try:
            with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
                return bool(response.status == 200)
        except OSError:
            return False

    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("本机演示服务提前退出，请检查构建与端口")
        if await asyncio.to_thread(probe):
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("本机演示服务未就绪")


async def seed_pages(url: URL) -> dict[str, str]:
    """复用已验收的 Fake 事故 Workflow；页面查询使用真实持久化结果。"""
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "127.0.0.1:7233")
    if url.host != "127.0.0.1" or not (url.database or "").startswith("weipai_db_test_"):
        raise ValueError("前端演示只能使用本机隔离测试库")
    with patch.dict(os.environ, {"TEST_TEMPORAL_ADDRESS": address}, clear=True):
        with redirect_stdout(io.StringIO()):
            await incident_demo(url)
    database = Database(url)
    try:
        async with database.session() as session:
            queries = ConsoleQueries(session)
            hit = (await queries.incidents(1, 0, "payment-service")).items[0]
            event = (await queries.events(1, 0, TaskSource.ALERT, None, "payment-service")).items[0]
            return {
                "WEIPAI_FRONTEND_TASK_ID": str(hit.report.task_id),
                "WEIPAI_FRONTEND_INCIDENT_ID": str(hit.evidence_id),
                "WEIPAI_FRONTEND_EVENT_ID": str(event.id),
            }
    finally:
        await database.dispose()


def wait_for_close() -> None:
    prompt = "检查完成后按 Enter 关闭服务并清理临时数据库："
    if sys.platform == "win32":
        # uv/ConPTY 下 getpass 从控制台读键，重定向 stdin 的 input 可能无法收到 Enter。
        # 与 Windows getpass 使用相同的控制台通道；只接收结束/取消按键。
        import msvcrt

        print(prompt, end="", flush=True)
        while True:
            key = msvcrt.getwch()
            if key in {"\r", "\n"}:
                print(flush=True)
                return
            if key == "\x03":
                raise KeyboardInterrupt
    else:
        input(prompt)


async def seed_cognition(url: URL) -> None:
    database = Database(url)
    try:
        async with database.session() as session:
            catalog = CatalogService(session, Settings(APP_ENV="test", LLM_MODE="fake"))
            await catalog.knowledge(
                "create",
                KnowledgeInput.model_validate_json(
                    json.dumps(
                        {
                            "kind": "business_rule",
                            "content": "支付核心链路优先保障，回滚后必须独立验证。",
                            "source": "本人本机认知页面验收样例",
                        }
                    )
                ),
                None,
                "local-browser-seed",
            )
            await catalog.runbook(
                "create",
                RunbookInput.model_validate_json(
                    payment_runbook("payment-cognition-demo").model_dump_json(
                        include=set(RunbookContent.model_fields)
                    )
                ),
                None,
                "local-browser-seed",
            )
    finally:
        await database.dispose()


async def verify_cognition(url: URL) -> None:
    database = Database(url)
    try:
        async with database.session() as session:
            records = (
                await session.scalars(
                    select(CatalogAudit).where(CatalogAudit.actor == "local-browser-owner")
                )
            ).all()
            assert len(records) == 6
            assert {record.operation for record in records} == {
                f"{kind}.{verb}"
                for kind in ("knowledge", "runbooks")
                for verb in ("create", "update", "delete")
            }
            for record in records:
                model = KnowledgeEntry if record.operation.startswith("knowledge.") else Runbook
                assert await session.get(model, UUID(str(record.details["record_id"]))) is None
                assert record.occurred_at.utcoffset() is not None
        print(
            "跨会话数据库核对通过：两类内容各新建/编辑/删除，六条本人审计，删除后记录不存在。",
            flush=True,
        )
    finally:
        await database.dispose()


async def run_demo(
    url: URL,
    *,
    interactive: bool = False,
    approvals: bool = False,
    cognition: bool = False,
    operations: bool = False,
    dashboard: bool = False,
    chat: bool = False,
) -> None:
    if chat:
        async with chat_scenario(url) as settings:
            await run_browser(url, interactive=interactive, chat=settings)
    elif approvals or dashboard:
        async with approval_scenario(url) as scenario:
            await run_browser(url, interactive=interactive, scenario=scenario, dashboard=dashboard)
    else:
        await run_browser(url, interactive=interactive, cognition=cognition, operations=operations)


async def seed_operations(url: URL) -> dict[str, str]:
    """复用已验收场景，持久化报告来自真实本机 Temporal + Fake 闭环。"""
    from temporalio.client import Client

    from app.connectors.feishu.fake import FakeFeishuConnector
    from app.connectors.inspection.fake import sample_facts
    from app.learning.automation.demo import run_demo as automation_demo
    from app.tasks.architecture.demo import run_demo as architecture_demo
    from app.tasks.inspection.demo import case as inspection_case
    from app.tasks.releases.demo import run_demo as release_demo
    from app.tasks.tickets.demo import run_demo as ticket_demo
    from app.tasks.war_room.demo import run_demo as war_room_demo

    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if address.rpartition(":")[0] != "127.0.0.1":
        raise ValueError("运营页面验收只允许本机 Temporal")
    # 不继承个人 Connector/网关/权限配置；各演示已限定临时库并自行清理 Workflow。
    for label, demo in (
        ("自动化建议", automation_demo),
        ("发布", release_demo),
        ("工单", ticket_demo),
        ("架构评审", architecture_demo),
        ("重大保障", war_room_demo),
    ):
        print(f"正在准备{label}的 Fake 场景与真实报告…", flush=True)
        with patch.dict(os.environ, {"TEST_TEMPORAL_ADDRESS": address}, clear=True):
            with redirect_stdout(io.StringIO()):
                await demo(url)
    database = Database(url)
    try:
        print("正在运行 Fake 巡检，生成四条持久化风险…", flush=True)
        client = await Client.connect(address)
        with patch.dict(os.environ, {"TEST_TEMPORAL_ADDRESS": address}, clear=True):
            await inspection_case(
                database, client, sample_facts("payment-service"), FakeFeishuConnector()
            )
        async with database.session() as session:
            queries = OperationsQueries(session)
            risks = await queries.risks(20, 0, "payment-service", True, None)
            assert risks.total == 4
            inspections = await queries.scenarios("inspections", 20, 0, "payment-service", None)
            assert inspections.total == 1
            task = inspections.items[0].task
            detail = await queries.scenario("inspections", task.id)
            assert any(e.source_tool == "inspection.report" for e in detail.evidence)
            return {
                "WEIPAI_FRONTEND_INSPECTION_ID": str(task.id),
                "WEIPAI_FRONTEND_RISK_ID": str(risks.items[0].id),
            }
    finally:
        await database.dispose()


async def run_browser(
    url: URL,
    *,
    interactive: bool = False,
    scenario: ApprovalPageScenario | None = None,
    cognition: bool = False,
    operations: bool = False,
    dashboard: bool = False,
    chat: Settings | None = None,
) -> None:
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("未找到 Node.js")
    password = secrets.token_urlsafe(24)
    if interactive:
        from getpass import getpass

        password = getpass("设置临时浏览器验收密码（12–256 字符，不显示、不保存）：")
        if not 12 <= len(password) <= 256:
            raise ValueError("临时密码必须为 12–256 字符")
    print("正在复用既有 Fake Workflow 准备事故、事件、结论和证据…", flush=True)
    identities = await seed_pages(url)
    if cognition or dashboard:
        await seed_cognition(url)
    if operations:
        identities.update(await seed_operations(url))
    if scenario:
        print("正在准备审批、判断、补充信息和接管的真实等待任务…", flush=True)
        identities.update(await scenario.seed())
    api_port = free_port()
    frontend_port = free_port()
    api_origin = f"http://127.0.0.1:{api_port}"
    frontend_origin = f"http://127.0.0.1:{frontend_port}"
    # 子进程只继承系统运行参数和明确的本机配置，不继承个人部署/网关凭证。
    environment = dict(
        {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC"}
        },
        APP_ENV="test",
        CONNECTOR_MODE="fake",
        LLM_MODE="fake",
        API_HOST="127.0.0.1",
        API_PORT=str(api_port),
        DATABASE_URL=url.render_as_string(hide_password=False),
        AUTH_CONFIG=json.dumps(
            {
                "username": "local-browser-owner",
                "password_hash": hash_password(password),
                "session_secret": secrets.token_hex(32),
                "public_origin": frontend_origin,
            }
        ),
    )
    if chat:
        environment["TEMPORAL_CONFIG"] = chat.temporal_config.model_dump_json()
    if scenario:
        environment["TEMPORAL_CONFIG"] = scenario.settings.temporal_config.model_dump_json()
    processes: list[subprocess.Popen[bytes]] = []
    try:
        processes.append(
            subprocess.Popen(
                [sys.executable, "-m", "app.api.main"],
                cwd=root / "backend",
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        )
        await ready(processes[0], api_origin + "/health")
        processes.append(
            subprocess.Popen(
                [
                    node,
                    str(root / "frontend/node_modules/vite/bin/vite.js"),
                    "preview",
                    "--port",
                    str(frontend_port),
                ],
                cwd=root / "frontend",
                env=dict(os.environ, WEIPAI_API_TARGET=api_origin),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        )
        await ready(processes[1], frontend_origin)
        await asyncio.to_thread(
            subprocess.run,
            [
                node,
                str(root / "frontend/node_modules/@playwright/test/cli.js"),
                "test",
                "shell.spec.ts",
                "tasks.spec.ts",
                *(
                    ["chat.spec.ts"]
                    if chat
                    else ["dashboard.spec.ts"]
                    if dashboard
                    else ["approvals.spec.ts"]
                    if scenario
                    else ["cognition.spec.ts"]
                    if cognition
                    else ["operations.spec.ts"]
                    if operations
                    else []
                ),
            ],
            cwd=root / "frontend",
            env=dict(
                os.environ,
                WEIPAI_FRONTEND_SMOKE_URL=frontend_origin,
                WEIPAI_FRONTEND_SMOKE_PASSWORD=password,
                **identities,
            ),
            check=True,
        )
        if scenario and not dashboard:
            await scenario.verify(identities)
        if cognition:
            await verify_cognition(url)
        print(
            "Step 53 真实浏览器通过：SSE 问答/追问、原 Evidence、Human 任务、"
            "L3 审批门禁与手机布局；"
            "外壳与任务页面回归通过。"
            if chat
            else "Step 52 真实浏览器通过：总览待处理/审批一致、十项指标原值、审计联合筛选与证据；"
            "外壳与任务页面回归通过。"
            if dashboard
            else "Step 51 真实浏览器通过：七类运营列表/详情、报告与 Evidence、巡检产生的四条风险；"
            "外壳与任务页面回归通过。"
            if operations
            else "Step 50 真实浏览器通过：图关系、悬停信息、Runbook/Knowledge 增删改与手机布局；"
            "外壳与任务页面回归通过。"
            if cognition
            else "Step 49 真实浏览器通过：审批、判断、补充信息和接管；外壳与任务页面回归通过。"
            if scenario
            else "Step 48 真实浏览器通过：任务筛选、状态时间线、证据、事故和事件；外壳回归通过。",
            flush=True,
        )
        if interactive:
            if scenario and not dashboard:
                identities.update(await scenario.seed())
            entry = (
                "chat?service=payment-service&start=2026-10-01T01:00:00Z&end=2026-10-01T02:00:00Z"
                if chat
                else "dashboard"
                if dashboard
                else "inspections"
                if operations
                else "services"
                if cognition
                else "approvals"
                if scenario
                else "tasks"
            )
            print(f"\n浏览器地址：{frontend_origin}/{entry}", flush=True)
            print("账户：local-browser-owner；密码为刚才设置的临时密码。", flush=True)
            print(
                f"事故任务：{identities['WEIPAI_FRONTEND_TASK_ID']}（CLOSED / Alert）。", flush=True
            )
            print(
                "可检查任务详情、证据链接、事故十三章与事件关联；数据仅在临时本机库。", flush=True
            )
            if scenario and not dashboard:
                for kind, label in (
                    ("APPROVAL", "批准"),
                    ("REJECT", "拒绝"),
                    ("JUDGMENT", "人工判断"),
                    ("INFORMATION", "补充信息"),
                    ("TAKEOVER", "接管"),
                ):
                    print(
                        f"{label}样例：{frontend_origin}/approvals/{identities[f'WEIPAI_FRONTEND_{kind}_ID']}",
                        flush=True,
                    )
                print("本步批准后保持真实 EXECUTING 授权交接状态，运维执行为 0。", flush=True)
            if chat:
                print(
                    "可输入支付故障问题，等待逐段回答，点击 Evidence，再到任务中心筛选 Human。\n"
                    "保持样例 UTC 时间窗；只读问答可追问。处置模式回滚会等待审批，审批前执行 0。\n"
                    "仅使用 payment-service 固定 Fake 样例；临时库/Worker 在 Enter 退出后清理。",
                    flush=True,
                )
            if cognition:
                print(f"服务关系图：{frontend_origin}/services/payment-service", flush=True)
                print(
                    "可悬停/选择关系，检查来源、置信度和新鲜度；在运行手册与知识中心新建、编辑、删除后刷新页面核对。",
                    flush=True,
                )
                print(
                    "样例手册 payment-cognition-demo；样例知识‘支付核心链路优先保障’。"
                    "全部数据只在本机临时库。",
                    flush=True,
                )
            if dashboard:
                print(
                    "总览的三类等待数量可分别与审批中心核对；十项指标可展开查看原值与统计依据。\n"
                    f"审计地址：{frontend_origin}/audit?actor=local-browser-seed&event_type=catalog_edit\n"
                    "样例包含真实 Fake 事故、等待任务和两条内容编辑审计；"
                    "可按 UTC 时间窗/类型/操作人筛选。",
                    flush=True,
                )
            if operations:
                print(
                    f"巡检详情：{frontend_origin}/inspections/{identities['WEIPAI_FRONTEND_INSPECTION_ID']}\n"
                    f"风险中心：{frontend_origin}/risks?service_name=payment-service\n"
                    "七个运营入口均有 Fake 样例；巡检 CLOSED，四条风险仍未恢复。\n"
                    "可检查报告、点击 Evidence、筛选/刷新/返回与手机布局。\n"
                    "浏览器只读；准备样例时的动作均为既有 Fake Connector 内执行。",
                    flush=True,
                )
            await asyncio.to_thread(wait_for_close)
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
