"""证明闭环网络门禁在连接前拒绝外部系统和未授权本机端口。"""

import socket

import httpx2 as httpx
import pytest

from tests.e2e_support import NetworkGuard, require_local_temporal


@pytest.mark.parametrize("address", [("203.0.113.1", 443), ("127.0.0.1", 6443)])
def test_socket_and_dns_block_before_connect(
    monkeypatch: pytest.MonkeyPatch, address: tuple[str, int]
) -> None:
    guard = NetworkGuard(frozenset({("127.0.0.1", 5432), ("127.0.0.1", 7233)}))
    guard.install(monkeypatch)
    with socket.socket() as client:
        with pytest.raises(AssertionError, match="禁止访问"):
            client.connect(address)
        with pytest.raises(AssertionError, match="禁止访问"):
            client.connect_ex(address)
    with pytest.raises(AssertionError, match="禁止访问"):
        socket.getaddrinfo(*address)
    assert len(guard.rejected) == 3


@pytest.mark.asyncio
async def test_real_http_transport_blocked_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = NetworkGuard(frozenset())
    guard.install(monkeypatch)
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(AssertionError, match="ASGITransport"):
            await client.get("https://example.invalid")
    with httpx.Client(trust_env=False) as sync_client:
        with pytest.raises(AssertionError, match="ASGITransport"):
            sync_client.get("https://example.invalid")
    assert guard.rejected == ["HTTP", "HTTP"]


@pytest.mark.parametrize("address", ["example.invalid:7233", "127.0.0.1:0", "127.0.0.1:65536"])
def test_temporal_native_transport_requires_local_endpoint(address: str) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        require_local_temporal(address)
