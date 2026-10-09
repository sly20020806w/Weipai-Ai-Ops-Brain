"""隔离测试与宿主机的应用配置。"""

import socket
from collections.abc import AsyncIterator

import httpx2 as httpx
import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APP_ENV",
        "AGENT_CONFIG",
        "CHAT_STREAM_TIMEOUT_SECONDS",
        "API_HOST",
        "API_PORT",
        "AUTH_CONFIG",
        "DATABASE_URL",
        "TEMPORAL_CONFIG",
        "TRIGGER_CONFIG",
        "SCHEDULING_CONFIG",
        "DETECTION_CONFIG",
        "DISCOVERY_CONFIG",
        "POLICY_CONFIG",
        "RUNBOOK_MATURITY_CONFIG",
        "VERIFICATION_CONFIG",
        "EXECUTION_CONFIG",
        "SAFETY_CONFIG",
        "AUTOMATION_CONFIG",
        "TICKET_CONFIG",
        "RELEASE_CONFIG",
        "INSPECTION_CONFIG",
        "WAR_ROOM_CONFIG",
        "INSPECTION_ENDPOINT",
        "CONNECTOR_MODE",
        "CONNECTOR_READER_TOKENS",
        "OPS_PLATFORM_CONFIG",
        "KUBERNETES_CONFIG",
        "PROMETHEUS_CONFIG",
        "SLS_CONFIG",
        "ARMS_CONFIG",
        "GIT_CONFIG",
        "CI_CONFIG",
        "ARGOCD_CONFIG",
        "CONFIG_CENTER_CONFIG",
        "CLOUD_CONFIG",
        "HOLMES_CONFIG",
        "FEISHU_CONFIG",
        "FEISHU_NOTIFICATION_CREDENTIALS",
        "LLM_MODE",
        "AI_GATEWAY_BASE_URL",
        "AI_GATEWAY_API_KEY",
        "AI_GATEWAY_CHAT_MODEL",
        "AI_GATEWAY_EMBEDDING_MODEL",
        "AI_GATEWAY_TIMEOUT_SECONDS",
        "AI_GATEWAY_MAX_RETRIES",
        "AI_GATEWAY_RETRY_DELAY_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest_asyncio.fixture
async def forbid_llm_network(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """网关与 Fake 测试禁止真实 HTTP transport、DNS 与 socket 连接。"""
    attempts: list[str] = []

    def blocked(*args: object, **kwargs: object) -> None:
        attempts.append("network")
        raise AssertionError("Step 7 测试禁止实际网络连接")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    yield
    assert attempts == [], "测试曾尝试绕过 MockTransport 访问网络"
