"""在项目已有本地 PostgreSQL 容器中新建临时库，完成验收后清理。"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import URL
from temporalio.service import __version__ as temporal_version
from temporalio.testing import WorkflowEnvironment

from app.db.session import Database
from app.knowledge.demo import run_demo


def docker(*arguments: str) -> str:
    return subprocess.run(
        ["docker", *arguments],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


async def verify_connection(url: URL) -> None:
    instance = Database(url)
    try:
        async with instance.engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
    finally:
        await instance.dispose()


async def knowledge_demo(url: URL) -> None:
    instance = Database(url)
    try:
        await run_demo(instance)
    finally:
        await instance.dispose()


async def prepare_time_skipping_server(root: Path) -> str:
    cache = root / ".cache" / "temporal-test"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / (
        f"temporal-test-server-sdk-python-{temporal_version}"
        + (".exe" if sys.platform == "win32" else "")
    )
    if not binary.is_file():
        # 首次准备官方测试工具；pytest 只使用已缓存的本地二进制，不在测试中下载。
        async with await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(cache)):
            pass
    if not binary.is_file():
        raise RuntimeError("未找到官方 Temporal 时间跳跃服务器缓存")
    return str(binary)


def main() -> None:
    parser = argparse.ArgumentParser(description="独立本地数据库/Temporal 验收")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--workflow", action="store_true")
    modes.add_argument("--discovery", action="store_true")
    modes.add_argument("--timeline", action="store_true")
    modes.add_argument("--knowledge", action="store_true")
    modes.add_argument("--knowledge-demo", action="store_true")
    modes.add_argument("--events", action="store_true")
    modes.add_argument("--events-demo", action="store_true")
    modes.add_argument("--schedules", action="store_true")
    modes.add_argument("--schedules-demo", action="store_true")
    modes.add_argument("--detection", action="store_true")
    modes.add_argument("--detection-demo", action="store_true")
    modes.add_argument("--agent", action="store_true")
    modes.add_argument("--agent-demo", action="store_true")
    modes.add_argument("--runbooks", action="store_true")
    modes.add_argument("--runbooks-demo", action="store_true")
    modes.add_argument("--experts", action="store_true")
    modes.add_argument("--experts-demo", action="store_true")
    modes.add_argument("--reviewer", action="store_true")
    modes.add_argument("--reviewer-demo", action="store_true")
    modes.add_argument("--action-plans", action="store_true")
    modes.add_argument("--action-plans-demo", action="store_true")
    modes.add_argument("--human", action="store_true")
    modes.add_argument("--human-demo", action="store_true")
    modes.add_argument("--approval", action="store_true")
    modes.add_argument("--approval-demo", action="store_true")
    modes.add_argument("--verifier", action="store_true")
    modes.add_argument("--verifier-demo", action="store_true")
    modes.add_argument("--executor", action="store_true")
    modes.add_argument("--executor-demo", action="store_true")
    modes.add_argument("--safety", action="store_true")
    modes.add_argument("--safety-demo", action="store_true")
    modes.add_argument("--postmortem", action="store_true")
    modes.add_argument("--postmortem-demo", action="store_true")
    modes.add_argument("--maturity", action="store_true")
    modes.add_argument("--maturity-demo", action="store_true")
    modes.add_argument("--replay", action="store_true")
    modes.add_argument("--replay-demo", action="store_true")
    modes.add_argument("--automation", action="store_true")
    modes.add_argument("--automation-demo", action="store_true")
    modes.add_argument("--tickets", action="store_true")
    modes.add_argument("--tickets-demo", action="store_true")
    modes.add_argument("--releases", action="store_true")
    modes.add_argument("--releases-demo", action="store_true")
    modes.add_argument("--inspections", action="store_true")
    modes.add_argument("--inspections-demo", action="store_true")
    modes.add_argument("--architecture", action="store_true")
    modes.add_argument("--architecture-demo", action="store_true")
    modes.add_argument("--war-room", action="store_true")
    modes.add_argument("--war-room-demo", action="store_true")
    modes.add_argument("--auth", action="store_true")
    modes.add_argument("--auth-demo", action="store_true")
    modes.add_argument("--console", action="store_true")
    modes.add_argument("--console-demo", action="store_true")
    modes.add_argument("--operations", action="store_true")
    modes.add_argument("--operations-demo", action="store_true")
    modes.add_argument("--chat", action="store_true")
    modes.add_argument("--e2e", action="store_true")
    modes.add_argument("--chat-demo", action="store_true")
    modes.add_argument("--frontend-smoke", action="store_true")
    modes.add_argument("--approval-pages", action="store_true")
    modes.add_argument("--cognition-pages", action="store_true")
    modes.add_argument("--operations-pages", action="store_true")
    modes.add_argument("--dashboard-pages", action="store_true")
    modes.add_argument("--chat-pages", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    container_ids = docker(
        "ps",
        "--quiet",
        "--filter",
        "label=com.docker.compose.project=weipai-ai-ops-brain-local",
        "--filter",
        "label=com.docker.compose.service=postgres",
    ).splitlines()
    if len(container_ids) != 1:
        raise RuntimeError("请先启动本项目的本地 PostgreSQL 依赖容器")
    container_id = container_ids[0]
    bindings = json.loads(docker("inspect", container_id))[0]["NetworkSettings"]["Ports"][
        "5432/tcp"
    ]
    if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
        raise RuntimeError("数据库验收要求 PostgreSQL 仅绑定本机 127.0.0.1")
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    database_name = f"weipai_db_test_{uuid4().hex}"
    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host="127.0.0.1",
        port=int(bindings[0]["HostPort"]),
        database=database_name,
    )

    def sql(statement: str) -> None:
        docker(
            "exec",
            "-i",
            container_id,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            user,
            "-d",
            "postgres",
            "-c",
            statement,
        )

    sql(f'CREATE DATABASE "{database_name}"')
    print("已创建独立的本地临时测试库（不输出凭证）", flush=True)
    try:
        asyncio.run(verify_connection(url))
        environment = dict(os.environ)
        environment.pop("DATABASE_URL", None)
        environment["TEST_DATABASE_URL"] = url.render_as_string(hide_password=False)
        environment["APP_ENV"] = "test"
        if (
            options.frontend_smoke
            or options.approval_pages
            or options.cognition_pages
            or options.operations_pages
            or options.dashboard_pages
            or options.chat_pages
        ):
            from demo_frontend import run_demo as frontend_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(
                frontend_demo(
                    url,
                    interactive=options.interactive,
                    approvals=options.approval_pages,
                    cognition=options.cognition_pages,
                    operations=options.operations_pages,
                    dashboard=options.dashboard_pages,
                    chat=options.chat_pages,
                )
            )
            return
        if options.chat_demo:
            from demo_chat import run_demo as chat_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(chat_demo(url, interactive=options.interactive))
            return
        if options.operations_demo:
            from demo_operations import run_demo as operations_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(operations_demo(url, interactive=options.interactive))
            return
        if options.console_demo:
            from demo_console import run_demo as console_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(console_demo(url, interactive=options.interactive))
            return
        if options.war_room_demo:
            from app.tasks.war_room.demo import run_demo as war_room_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(war_room_demo(url, interactive=options.interactive))
            return
        if options.architecture_demo:
            from app.tasks.architecture.demo import run_demo as architecture_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(architecture_demo(url))
            return
        if options.inspections_demo:
            from app.tasks.inspection.demo import run_demo as inspections_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(inspections_demo(url))
            return
        if options.releases_demo:
            from app.tasks.releases.demo import run_demo as releases_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(releases_demo(url, interactive=options.interactive))
            return
        if options.tickets_demo:
            from app.tasks.tickets.demo import run_demo as tickets_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(tickets_demo(url, interactive=options.interactive))
            return
        if options.automation_demo:
            from app.learning.automation.demo import run_demo as automation_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=url.render_as_string(hide_password=False)),
                check=True,
            )
            asyncio.run(automation_demo(url))
            return
        if options.replay_demo:
            from app.learning.evaluation.demo import run_demo as replay_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(replay_demo(url))
            return
        if options.maturity_demo:
            from app.runbooks.maturity_demo import run_demo as maturity_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(maturity_demo(url))
            return
        if options.postmortem_demo:
            from app.learning.demo import run_demo as postmortem_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(postmortem_demo(url))
            return
        if options.safety_demo:
            from app.tasks.safety.demo import run_demo as safety_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(safety_demo(url))
            return
        if options.executor_demo:
            from app.executor.demo import run_demo as executor_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(executor_demo(url, interactive=options.interactive))
            return
        if options.verifier_demo:
            from app.verifier.demo import run_demo as verifier_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(verifier_demo(url))
            return
        if options.approval_demo:
            from app.tasks.approval.demo import run_demo as approval_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(approval_demo(url, interactive=options.interactive))
            return
        if options.human_demo:
            from app.tasks.human.demo import run_demo as human_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(human_demo(url, interactive=options.interactive))
            return
        if options.action_plans_demo:
            from app.tasks.planning.demo import run_demo as action_plan_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(action_plan_demo(url))
            return
        if options.reviewer_demo:
            from app.agent.reviewer.demo import run_demo as reviewer_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(reviewer_demo(url))
            return
        if options.experts_demo:
            from app.agent.experts.demo import run_demo as experts_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(experts_demo(url))
            return
        if options.runbooks_demo:
            from app.runbooks.demo import run_demo as runbook_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(runbook_demo(url))
            return
        if options.agent_demo:
            from app.agent.demo import run_demo as agent_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(agent_demo(url))
            return
        if options.detection_demo:
            from app.triggers.detection.demo import run_demo as detection_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(detection_demo(url))
            return
        if options.schedules:
            environment["TEST_TIME_SKIPPING_SERVER"] = asyncio.run(
                prepare_time_skipping_server(root)
            )
        if options.schedules_demo:
            from app.triggers.scheduling.demo import run_demo as schedule_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(schedule_demo(url))
            return
        if options.events_demo:
            if not environment.get("TEST_TEMPORAL_ADDRESS"):
                raise ValueError("演示需要本地 Temporal 地址")
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            subprocess.run(
                [sys.executable, str(root / "scripts" / "demo_events.py")],
                cwd=root / "backend",
                env=environment,
                check=True,
            )
            return
        if options.knowledge_demo:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(knowledge_demo(url))
            return
        if options.auth_demo:
            from demo_auth import run_demo as auth_demo

            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root / "backend",
                env=dict(environment, DATABASE_URL=environment["TEST_DATABASE_URL"]),
                check=True,
            )
            asyncio.run(auth_demo(url))
            return
        tests = (
            ["tests/test_timeline.py", "tests/test_timeline_integration.py"]
            if options.timeline
            else (
                ["tests/test_discovery.py", "tests/test_discovery_integration.py"]
                if options.discovery
                else (
                    [
                        "tests/test_workflow.py",
                        "tests/test_workflow_activities_integration.py",
                        "tests/test_workflow_integration.py",
                    ]
                    if options.workflow
                    else [
                        "tests/test_database_integration.py",
                        "tests/test_tasks_integration.py",
                        "tests/test_ledger_integration.py",
                        "tests/test_graph_integration.py",
                        "tests/test_tools_integration.py",
                        "tests/test_workflow_activities_integration.py",
                        "tests/test_discovery_integration.py",
                        "tests/test_timeline_integration.py",
                        "tests/test_knowledge_integration.py",
                        "tests/test_events_integration.py",
                        "tests/test_scheduling_integration.py",
                        "tests/test_detection_integration.py",
                        "tests/test_main_agent_integration.py",
                        "tests/test_runbooks_integration.py",
                        "tests/test_experts_integration.py",
                        "tests/test_reviewer_integration.py",
                        "tests/test_action_plans_integration.py",
                        "tests/test_human_interaction_integration.py",
                        "tests/test_approval_integration.py",
                        "tests/test_verifier_integration.py",
                        "tests/test_executor_integration.py",
                        "tests/test_safety_integration.py",
                        "tests/test_postmortem_integration.py",
                        "tests/test_runbook_maturity_integration.py",
                        "tests/test_evaluation_integration.py",
                        "tests/test_automation_integration.py",
                        "tests/test_tickets_integration.py",
                        "tests/test_releases_integration.py",
                        "tests/test_inspection_integration.py",
                        "tests/test_architecture_integration.py",
                        "tests/test_war_room_integration.py",
                        "tests/test_auth_integration.py",
                        "tests/test_console_integration.py",
                        "tests/test_operations_integration.py",
                        "tests/test_chat_integration.py",
                    ]
                )
            )
        )
        if options.knowledge:
            tests = ["tests/test_knowledge.py", "tests/test_knowledge_integration.py"]
        if options.architecture:
            tests = ["tests/test_architecture.py", "tests/test_architecture_integration.py"]
        if options.war_room:
            tests = ["tests/test_war_room.py", "tests/test_war_room_integration.py"]
        if options.auth:
            tests = ["tests/test_auth.py", "tests/test_auth_integration.py"]
        if options.console:
            tests = ["tests/test_console.py", "tests/test_console_integration.py"]
        if options.operations:
            tests = ["tests/test_operations.py", "tests/test_operations_integration.py"]
        if options.chat:
            tests = ["tests/test_chat.py", "tests/test_chat_integration.py"]
        if options.e2e:
            tests = ["tests/test_e2e_guard.py", "tests/test_e2e_integration.py"]
        if options.events:
            tests = ["tests/test_events.py", "tests/test_events_integration.py"]
        if options.schedules:
            tests = ["tests/test_scheduling.py", "tests/test_scheduling_integration.py"]
        if options.detection:
            tests = ["tests/test_detection.py", "tests/test_detection_integration.py"]
        if options.agent:
            tests = ["tests/test_main_agent.py", "tests/test_main_agent_integration.py"]
        if options.runbooks:
            tests = ["tests/test_runbooks.py", "tests/test_runbooks_integration.py"]
        if options.experts:
            tests = ["tests/test_experts.py", "tests/test_experts_integration.py"]
        if options.reviewer:
            tests = ["tests/test_reviewer.py", "tests/test_reviewer_integration.py"]
        if options.action_plans:
            tests = [
                "tests/test_action_plans.py",
                "tests/test_action_plans_integration.py",
            ]
        if options.human:
            tests = [
                "tests/test_human_interaction.py",
                "tests/test_human_interaction_integration.py",
            ]
        if options.approval:
            tests = ["tests/test_approval.py", "tests/test_approval_integration.py"]
        if options.verifier:
            tests = ["tests/test_verifier.py", "tests/test_verifier_integration.py"]
        if options.executor:
            tests = ["tests/test_executor.py", "tests/test_executor_integration.py"]
        if options.safety:
            tests = ["tests/test_safety.py", "tests/test_safety_integration.py"]
        if options.postmortem:
            tests = ["tests/test_postmortem.py", "tests/test_postmortem_integration.py"]
        if options.maturity:
            tests = ["tests/test_runbook_maturity.py", "tests/test_runbook_maturity_integration.py"]
        if options.replay:
            tests = [
                "tests/test_evaluation.py",
                "tests/test_evaluation_integration.py",
                "tests/test_experts_integration.py::"
                "test_six_experts_query_via_dispatcher_and_consult_replays",
                "tests/test_reviewer_integration.py::"
                "test_reviewer_tools_replay_original_results_without_connectors",
                "tests/test_executor_integration.py::"
                "test_approved_rollback_audit_no_secret_and_verifying_only",
                "tests/test_runbook_maturity_integration.py::"
                "test_replay_does_not_increment_maturity",
            ]
        if options.automation:
            tests = ["tests/test_automation.py", "tests/test_automation_integration.py"]
        if options.tickets:
            tests = ["tests/test_tickets.py", "tests/test_tickets_integration.py"]
        if options.releases:
            tests = ["tests/test_releases.py", "tests/test_releases_integration.py"]
        if options.inspections:
            tests = ["tests/test_inspection.py", "tests/test_inspection_integration.py"]
        if (
            options.workflow
            or options.discovery
            or options.timeline
            or options.events
            or options.schedules
            or options.detection
            or options.agent
            or options.runbooks
            or options.experts
            or options.reviewer
            or options.action_plans
            or options.human
            or options.approval
            or options.verifier
            or options.executor
            or options.safety
            or options.postmortem
            or options.maturity
            or options.replay
            or options.automation
            or options.tickets
            or options.releases
            or options.inspections
            or options.architecture
            or options.war_room
            or options.console
            or options.chat
            or options.e2e
        ):
            if not environment.get("TEST_TEMPORAL_ADDRESS"):
                raise ValueError("请用专项验收脚本设置本地 Temporal 测试地址")
        e2e_report = root / ".cache" / "e2e" / f"{uuid4().hex}.xml"
        extra_arguments = ["-s", "--junitxml", str(e2e_report)] if options.e2e else []
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *tests,
                "-v",
                "--tb=short",
                *extra_arguments,
            ],
            cwd=root / "backend",
            env=environment,
            check=True,
        )
        if options.e2e:
            cases = ET.parse(e2e_report).findall(".//testcase")
            if len(cases) != 9 or any(
                case.find(kind) is not None
                for case in cases
                for kind in ("skipped", "failure", "error")
            ):
                raise RuntimeError("Step 54 必须实际通过全部 9 项验收，不允许跳过")
        if options.knowledge:
            asyncio.run(knowledge_demo(url))
    finally:
        sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        print("临时测试库已清理", flush=True)
    print(
        "Step 54 端到端闭环验收全部通过（9 项，零跳过）"
        if options.e2e
        else "Step 46 AI Chat API 验收全部通过"
        if options.chat
        else "Step 45 认知与运营 API 验收全部通过"
        if options.operations
        else "Step 44 任务闭环 API 验收全部通过"
        if options.console
        else "Step 43 鉴权验收全部通过"
        if options.auth
        else "Step 42 War Room 验收全部通过"
        if options.war_room
        else "Step 41 架构评审验收全部通过"
        if options.architecture
        else "Step 40 巡检与治理验收全部通过"
        if options.inspections
        else "Step 39 发布与变更场景验收全部通过"
        if options.releases
        else "Step 38 工单场景验收全部通过"
        if options.tickets
        else "Step 37 Automation Discovery 验收全部通过"
        if options.automation
        else "Step 36 Replay 与 AI 评价验收全部通过"
        if options.replay
        else "Step 35 Runbook 成熟度验收全部通过"
        if options.maturity
        else "Step 34 Postmortem 验收全部通过"
        if options.postmortem
        else "Step 33 自动熔断验收全部通过"
        if options.safety
        else "Step 32 Executor 验收全部通过"
        if options.executor
        else "Step 31 Verifier 验收全部通过"
        if options.verifier
        else "Step 30 审批流验收全部通过"
        if options.approval
        else "Step 29 人工判断与补充信息验收全部通过"
        if options.human
        else "Step 28 Action Plan 验收全部通过"
        if options.action_plans
        else "Step 27 Reviewer Agent 验收全部通过"
        if options.reviewer
        else "Step 26 专家 Agent 验收全部通过"
        if options.experts
        else "Step 25 Runbook Engine 验收全部通过"
        if options.runbooks
        else "Step 24 主 Agent 验收全部通过"
        if options.agent
        else "Step 23 状态与预测驱动验收全部通过"
        if options.detection
        else "Step 22 定时驱动验收全部通过"
        if options.schedules
        else "Step 21 事件接入验收全部通过"
        if options.events
        else "Step 20 Knowledge Brain 验收全部通过"
        if options.knowledge
        else "Step 19 Change Timeline 验收全部通过"
        if options.timeline
        else "Step 18 Discovery 验收全部通过"
        if options.discovery
        else "Step 17 Temporal Workflow 验收全部通过"
        if options.workflow
        else "数据库基础层、AI Task、Evidence Ledger、审计、Context Graph "
        "与 Tool Dispatcher、Workflow Activity、Change Timeline、Knowledge Brain 验收全部通过",
        flush=True,
    )


if __name__ == "__main__":
    main()
