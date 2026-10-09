"""隔离临时库上的真实回环 HTTP 验收；不连接生产、不发送飞书。"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from sqlalchemy.engine import URL

from app.auth.passwords import hash_password


async def run_demo(url: URL) -> None:
    password = secrets.token_urlsafe(24)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    config = {
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
            API_HOST="127.0.0.1",
            API_PORT=str(port),
            AUTH_CONFIG=json.dumps(config),
            DATABASE_URL=url.render_as_string(hide_password=False),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def request(
        path: str, body: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, object]]:
        data = json.dumps(body).encode() if body is not None else None
        call = Request(
            origin + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with opener.open(call, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("演示 API 提前退出")
            try:
                if (await asyncio.to_thread(request, "/health"))[0] == 200:
                    break
            except OSError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("演示 API 未就绪")
        before, _ = await asyncio.to_thread(request, "/api/auth/me")
        signed_in, result = await asyncio.to_thread(
            request,
            "/api/auth/login",
            {"username": "local-demo-owner", "password": password},
            {"X-Ops-Login": "1"},
        )
        after, _ = await asyncio.to_thread(request, "/api/auth/me")
        refused, _ = await asyncio.to_thread(request, "/api/auth/logout", {})
        signed_out, _ = await asyncio.to_thread(
            request, "/api/auth/logout", {}, {"X-CSRF-Token": str(result["csrf_token"])}
        )
        ended, _ = await asyncio.to_thread(request, "/api/auth/me")
        assert (before, signed_in, after, refused, signed_out, ended) == (
            401,
            200,
            200,
            403,
            204,
            401,
        )
        print(
            f"本机 HTTP：未登录 {before} → 登录 {signed_in} → 查询身份 {after} → "
            f"缺 CSRF {refused} → 退出 {signed_out} → 会话失效 {ended}",
            flush=True,
        )
        print("Step 43 登录演示通过；无生产调用、无运维动作。", flush=True)
    finally:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 10)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
