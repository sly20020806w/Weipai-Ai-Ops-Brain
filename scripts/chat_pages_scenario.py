"""Step 53 本机浏览器对话使用既有 Fake 主 Agent 与独立 Worker。"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus

from app.config import Settings
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.models import Evidence
from app.tasks.worker import create_worker


@asynccontextmanager
async def chat_scenario(url: URL) -> AsyncIterator[Settings]:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("对话页面验收仅允许本机临时库与 Temporal")
    # 构造配置时隔离个人规则；只携带明确的本机地址，不继承审批放行配置。
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            APP_ENV="test",
            CONNECTOR_MODE="fake",
            LLM_MODE="fake",
            TEMPORAL_CONFIG={"address": address, "task_queue": f"chat-pages-{uuid4().hex}"},
        )
    database = Database(url)
    client = await Client.connect(address)
    try:
        await DiscoveryActivities(database, settings).refresh(
            DiscoveryRequest(datetime(2026, 10, 1, 2, tzinfo=UTC).isoformat(), 3600)
        )
        await TimelineActivities(database, settings).collect(
            TimelineRequest("payment-service", "2026-10-01T01:00:00Z", "2026-10-01T02:00:00Z")
        )
        async with create_worker(client, database, settings):
            yield settings
    finally:
        # 仅处理这个唯一队列。包括浏览器断流时尚未返回 task_id 的接入 Workflow。
        async for execution in client.list_workflows(
            query=(
                f'TaskQueue = "{settings.temporal_config.task_queue}" '
                'AND ExecutionStatus = "Running"'
            )
        ):
            handle = client.get_workflow_handle(execution.id)
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 53 本机对话样例清理")
        async with database.session() as session:
            chat_tasks = (
                await session.scalars(
                    select(Evidence.task_id).where(Evidence.source_tool == "chat.request")
                )
            ).all()
            for task_id in chat_tasks:
                handle = client.get_workflow_handle(f"ai-task-{task_id}")
                if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                    await handle.terminate("Step 53 本机对话任务清理")
        await database.dispose()
