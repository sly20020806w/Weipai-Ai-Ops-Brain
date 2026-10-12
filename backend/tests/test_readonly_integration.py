"""Step 57 的本机 Worker 验收；使用 Fake 来源，不连接 ACK 或公司系统。"""

import asyncio
import os
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from temporalio.client import (
    Client,
    ScheduleActionExecutionStartWorkflow,
    ScheduleActionStartWorkflow,
)
from temporalio.worker import Replayer

from app.config import Settings
from app.db.session import Database
from app.graph.discovery.models import DiscoveryResult
from app.graph.discovery.sources import DiscoverySources, configured_sources
from app.graph.discovery.workflow import DiscoveryWorkflow
from app.graph.models import GraphEdge, GraphNode
from app.ledger.models import AuditRecord, Evidence
from app.tasks.worker import run_worker
from tests import test_e2e_integration as e2e_fixtures
from tests.database_support import get_test_database_url
from tests.e2e_support import NetworkGuard
from tests.test_readonly import cloud_settings

# 复用已验证的异步网络限域与隔离数据库夹具。
database = e2e_fixtures.database
network_guard = e2e_fixtures.network_guard

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("TEST_TEMPORAL_ADDRESS"),
        reason="check-ack.ps1 提供本机隔离依赖与 Fake 来源",
    ),
]


async def test_readonly_entry_schedule_discovery_replay_and_shutdown(
    database: Database,
    network_guard: NetworkGuard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = "readonly-test-" + uuid4().hex
    schedule_id = "readonly-test-" + uuid4().hex
    address = os.environ["TEST_TEMPORAL_ADDRESS"]
    settings = cloud_settings(
        DATABASE_URL=get_test_database_url(),
        TEMPORAL_CONFIG={"address": address, "task_queue": queue},
        DISCOVERY_CONFIG={"schedule_id": schedule_id, "interval_seconds": 86400},
    )

    async def fake_sources(
        stack: AsyncExitStack,
        configured: Settings,
        end: datetime,
    ) -> DiscoverySources:
        assert configured.read_only and configured.connector_mode.value == "real"
        return await configured_sources(stack, Settings(APP_ENV="test"), end)

    monkeypatch.setattr("app.graph.discovery.activities.configured_sources", fake_sources)
    ready = tmp_path / "ready"
    monkeypatch.setattr("app.tasks.readonly.READY_PATH", ready)
    client = await Client.connect(address)
    runner = asyncio.create_task(run_worker(settings))
    handle = client.get_schedule_handle(schedule_id)
    created = False
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                if runner.done():
                    await runner
                await asyncio.sleep(0.05)
        created = True
        description = await handle.describe()
        assert isinstance(description.schedule.action, ScheduleActionStartWorkflow)
        assert description.schedule.action.task_queue == queue
        await handle.trigger()
        async with asyncio.timeout(30):
            while not (description := await handle.describe()).info.recent_actions:
                await asyncio.sleep(0.05)
        action = description.info.recent_actions[-1].action
        assert isinstance(action, ScheduleActionExecutionStartWorkflow)
        workflow = client.get_workflow_handle(action.workflow_id, result_type=DiscoveryResult)
        result = await asyncio.wait_for(workflow.result(), 30)
        assert result.node_count == 25 and result.edge_count == 31
        history = await workflow.fetch_history()
        await Replayer(workflows=[DiscoveryWorkflow]).replay_workflow(history)
        assert "offline-reader-token" not in history.to_json()
        async with database.session() as session:
            assert await session.scalar(select(func.count()).select_from(GraphNode)) == 25
            assert await session.scalar(select(func.count()).select_from(GraphEdge)) == 31
            assert all(
                e.source and e.last_seen.tzinfo is not None
                for e in await session.scalars(select(GraphEdge))
            )
            assert await session.scalar(select(func.count()).select_from(Evidence)) == 0
            assert await session.scalar(select(func.count()).select_from(AuditRecord)) == 0
        print(
            "只读 Worker → Temporal Schedule → Fake Discovery：25 节点/31 关系；"
            "真实运维及外网请求 0"
        )
    finally:
        if created:
            await handle.pause(note="本机 Step 57 Fake 验收结束")
            await handle.delete()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert not ready.exists()
