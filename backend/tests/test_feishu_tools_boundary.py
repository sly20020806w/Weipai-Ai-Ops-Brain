"""注册全部既有高级 Tool 后，飞书通知仍不能由 Agent 调用。"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.changes.fake import (
    FakeArgoCDConnector,
    FakeCIConnector,
    FakeConfigCenterConnector,
    FakeGitConnector,
)
from app.connectors.cloud.fake import FakeCloudConnector
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.kubernetes.fake import FakeKubernetesConnector
from app.connectors.observability.fake import (
    FakeARMSConnector,
    FakePrometheusConnector,
    FakeSLSConnector,
)
from app.connectors.ops_platform.fake import FakeOpsPlatformConnector
from app.graph.changes.service import TimelineService
from app.graph.service import GraphService
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyEnvironment
from app.tools.changes import register_change_tools
from app.tools.cloud import register_cloud_tools
from app.tools.dispatcher import ToolDispatcher
from app.tools.graph import register_graph_tools
from app.tools.kubernetes import register_kubernetes_tools
from app.tools.models import DispatchStatus
from app.tools.observability import register_observability_tools
from app.tools.ops_platform import register_ops_platform_tools
from app.tools.registry import ToolRegistry
from app.tools.timeline import register_timeline_tools
from tests.test_tools import MemoryLedger

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


@pytest.mark.asyncio
async def test_populated_tool_registry_has_no_feishu_tool_or_notification_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    register_ops_platform_tools(registry, FakeOpsPlatformConnector())
    register_kubernetes_tools(registry, FakeKubernetesConnector())
    register_observability_tools(
        registry, FakePrometheusConnector(), FakeSLSConnector(), FakeARMSConnector()
    )
    register_change_tools(
        registry,
        FakeGitConnector(),
        FakeCIConnector(),
        FakeArgoCDConnector(),
        FakeConfigCenterConnector(),
    )
    register_cloud_tools(registry, FakeCloudConnector())
    names = {tool.name for tool in registry.declarations()}
    assert len(names) == 12
    assert not any("feishu" in name or "send" in name or "notify" in name for name in names)
    async with FakeFeishuConnector() as notifier, AsyncSession() as session:
        register_graph_tools(registry, GraphService(session))
        register_timeline_tools(registry, TimelineService(session))
        all_names = {tool.name for tool in registry.declarations()}
        assert len(all_names) == 15
        assert "get_recent_changes" in all_names
        assert not any("feishu" in name or "send" in name or "notify" in name for name in all_names)
        async with session.begin():
            monkeypatch.setattr(session, "get", AsyncMock(return_value=object()))
            ledger = MemoryLedger(session)
            dispatcher = ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger)
            for tool_name in ("send_feishu_message", "send_feishu_card", "send_text", "send_card"):
                result = await dispatcher.dispatch(
                    task_id=uuid4(), tool_name=tool_name, parameters={}, actor="fake-agent"
                )
                assert result.status is DispatchStatus.REJECTED
                assert result.error_code == "tool_not_found"
            assert notifier.sent_messages == ()
            assert ledger.evidence == [] and len(ledger.audits) == 4
