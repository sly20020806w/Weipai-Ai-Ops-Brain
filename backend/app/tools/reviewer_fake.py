"""验收用 Reviewer Tool 组合；显式注入反证事实，禁止用于真实环境。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.observability.fake import (
    FakeARMSConnector,
    FakePrometheusConnector,
    FakeSLSConnector,
)
from app.connectors.observability.reviewer_fake import reviewer_traces
from app.graph.changes.service import TimelineService
from app.tools.observability import register_observability_tools
from app.tools.registry import ToolRegistry
from app.tools.timeline import register_timeline_tools


@asynccontextmanager
async def fake_review_registry(
    settings: Settings,
    session: AsyncSession,
    *,
    mode: Literal["clear", "contradicted"] = "clear",
) -> AsyncIterator[ToolRegistry]:
    if settings.app_env not in {"local", "test"} or settings.connector_mode.value != "fake":
        raise ValueError("反证演示只允许本地 Fake")
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        prometheus = await stack.enter_async_context(FakePrometheusConnector())
        sls = await stack.enter_async_context(FakeSLSConnector())
        arms = await stack.enter_async_context(FakeARMSConnector(reviewer_traces(mode)))
        register_observability_tools(registry, prometheus, sls, arms)
        register_timeline_tools(registry, TimelineService(session))
        yield registry
