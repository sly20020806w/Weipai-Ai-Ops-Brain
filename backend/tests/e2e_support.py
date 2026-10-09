"""闭环验收只允许 PostgreSQL/Temporal 的两个本机端口；HTTP 一律走 ASGI。"""

import socket
from dataclasses import dataclass, field

import httpx2 as httpx
import pytest


@dataclass
class NetworkGuard:
    endpoints: frozenset[tuple[str, int]]
    rejected: list[str] = field(default_factory=list)

    def check(self, address: object) -> None:
        if not isinstance(address, tuple) or address[:2] not in self.endpoints:
            self.rejected.append("socket/DNS")
            raise AssertionError("E2E 禁止访问本机数据库/Temporal 以外的网络")

    def block_http(self, *args: object, **kwargs: object) -> None:
        self.rejected.append("HTTP")
        raise AssertionError("E2E HTTP 只允许 ASGITransport，不允许真实网络 transport")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
        original_resolve = socket.getaddrinfo

        def connect(instance: socket.socket, address: object) -> None:
            self.check(address)
            assert isinstance(address, tuple)
            original_connect(instance, address)

        def connect_ex(instance: socket.socket, address: object) -> int:
            self.check(address)
            assert isinstance(address, tuple)
            return original_connect_ex(instance, address)

        def resolve(host: str, port: int, *args: object, **kwargs: object) -> object:
            self.check((host, port))
            return original_resolve(host, port, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", self.block_http)
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", self.block_http)


def require_local_temporal(address: str) -> int:
    host, _, raw_port = address.rpartition(":")
    if host != "127.0.0.1" or not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535:
        raise ValueError("E2E Temporal 必须为 127.0.0.1:端口")
    return int(raw_port)
