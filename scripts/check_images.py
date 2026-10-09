"""Step 56 镜像验收；只使用本项目本机容器、临时库和独立任务队列。"""

import argparse
import getpass
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.engine import URL

from app.auth.passwords import hash_password

ROOT = Path(__file__).resolve().parents[1]
BACKEND = "weipai-backend:step56"
FRONTEND = "weipai-frontend:step56"
PROJECT = "weipai-ai-ops-brain-local"

# 只作验收网络转发，让容器中的 localhost 仍保留应用的本机门禁。
RELAY = """
import asyncio, sys
async def forward(reader, writer, host, port):
    remote_reader, remote_writer = await asyncio.open_connection(host, port)
    async def copy(source, target):
        try:
            while data := await source.read(65536):
                target.write(data)
                await target.drain()
        finally:
            target.close()
    await asyncio.gather(copy(reader, remote_writer), copy(remote_reader, writer))
async def main():
    servers = []
    for host, port in [(sys.argv[1], 5432), (sys.argv[2], 7233)]:
        servers.append(await asyncio.start_server(
            lambda r, w, h=host, p=port: forward(r, w, h, p), '127.0.0.1', port))
    print('relay-ready', flush=True)
    await asyncio.gather(*(server.serve_forever() for server in servers))
asyncio.run(main())
"""

HTTP_CHECK = """
import http.cookiejar, json, os, urllib.request, urllib.error
base = 'http://127.0.0.1:8080'
origin = os.environ['IMAGE_TEST_ORIGIN']
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def request(path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data,
        headers={'Host': origin.removeprefix('http://'), **(headers or {})})
    try:
        with opener.open(req, timeout=10) as response:
            print('HTTP', path, response.status, flush=True)
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        print('HTTP', path, error.code, flush=True)
        return error.code, error.read(), dict(error.headers)
assert request('/health')[0] == 200
for path in ['/', '/ai-chat', '/tasks/example']:
    status, body, _ = request(path)
    assert status == 200 and b'<div id="root">' in body
assert request('/assets/missing.js')[0] == 404
assert request('/api/auth/me')[0] == 401
status, body, headers = request('/api/auth/login',
    {'username': 'image-check', 'password': os.environ['IMAGE_TEST_PASSWORD']},
    {'Origin': origin, 'X-Ops-Login': '1', 'Content-Type': 'application/json'})
assert status == 200, status
assert 'HttpOnly' in {key.lower(): value for key, value in headers.items()}['set-cookie']
csrf = json.loads(body)['csrf_token']
assert request('/api/auth/me')[0] == 200
assert request('/api/auth/logout', {},
    {'Origin': origin, 'Content-Type': 'application/json'})[0] == 403
assert request('/api/auth/logout', {},
    {'Origin': origin, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf})[0] == 204
assert request('/api/auth/me')[0] == 401
print('frontend-spa-login-csrf-logout-ok')
"""


def docker(
    *arguments: str, environment: dict[str, str] | None = None, data: str | None = None
) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        cwd=ROOT,
        env=environment,
        input=data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    if result.returncode:
        # 不输出 inspect 环境变量、凭证或容器日志。
        if data == HTTP_CHECK:
            print(result.stdout, flush=True)
            print(result.stderr.splitlines()[-1] if result.stderr else "HTTP 验收异常", flush=True)
        raise RuntimeError(f"Docker 验收命令失败：{arguments[0]}（退出码 {result.returncode}）")
    return result.stdout.strip()


def inspect_container(name: str) -> dict[str, Any]:
    item: dict[str, Any] = json.loads(docker("inspect", name))[0]
    return item


def project_container(service: str) -> dict[str, Any]:
    ids = docker(
        "ps",
        "--quiet",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--filter",
        f"label=com.docker.compose.service={service}",
    ).splitlines()
    if len(ids) != 1:
        raise ValueError(f"请先启动本项目唯一的本机 {service} 容器")
    return inspect_container(ids[0])


def wait_for(check: Callable[[], bool], label: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if check():
            print(f"通过：{label}", flush=True)
            return
        time.sleep(1)
    raise RuntimeError(f"验收等待超时：{label}")


def build_images() -> None:
    for image, filename in ((BACKEND, "backend"), (FRONTEND, "frontend")):
        print(f"构建：{image}", flush=True)
        subprocess.run(
            [
                "docker",
                "build",
                "--file",
                f"deploy/images/{filename}.Dockerfile",
                "--tag",
                image,
                ".",
            ],
            cwd=ROOT,
            check=True,
        )


def check_images(*, interactive: bool = False) -> dict[str, Any]:
    endpoint = (
        os.environ.get("DOCKER_HOST")
        or json.loads(docker("context", "inspect"))[0]["Endpoints"]["docker"]["Host"]
    )
    if not endpoint.startswith(("npipe://", "unix://")):
        raise ValueError("镜像验收只允许本机 Docker socket")
    postgres, temporal, admin = (
        project_container(name) for name in ("postgres", "temporal", "temporal-admin")
    )
    for item, port in ((postgres, "5432/tcp"), (temporal, "7233/tcp")):
        bindings = item["NetworkSettings"]["Ports"][port]
        if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
            raise ValueError("验收依赖必须只发布到本机回环地址")
    networks = set(postgres["NetworkSettings"]["Networks"]) & set(
        temporal["NetworkSettings"]["Networks"]
    )
    if len(networks) != 1:
        raise ValueError("本项目 PostgreSQL/Temporal 必须在唯一共同网络")
    network = networks.pop()
    variables = dict(entry.split("=", 1) for entry in postgres["Config"]["Env"])
    user, password = variables["POSTGRES_USER"], variables["POSTGRES_PASSWORD"]
    prefix = "image-check-" + uuid4().hex
    database = "weipai_image_test_" + uuid4().hex
    queue, schedule = prefix + "-queue", prefix + "-discovery"
    containers: list[str] = []
    schedule_attempted = False

    def sql(statement: str) -> None:
        docker(
            "exec",
            "-i",
            postgres["Id"],
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            user,
            "-d",
            "postgres",
            data=statement,
        )

    def run(
        name: str,
        image: str,
        *args: str,
        env: dict[str, str] | None = None,
        namespace: str | None = None,
        entrypoint: str | None = None,
    ) -> str:
        arguments = [
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"weipai.image-check={prefix}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            namespace or network,
        ]
        if namespace is None:
            arguments += ["--publish", "127.0.0.1::8080"]
        if entrypoint:
            arguments += ["--entrypoint", entrypoint]
        for key in env or {}:
            arguments += ["--env", key]
        containers.append(name)
        return docker(*arguments, image, *args, environment=dict(os.environ, **(env or {})))

    sql(f'CREATE DATABASE "{database}";')
    print("已创建镜像验收临时库（不打印凭证）", flush=True)
    try:
        relay = run(
            prefix + "-relay",
            BACKEND,
            "-u",
            "-c",
            RELAY,
            postgres["NetworkSettings"]["Networks"][network]["IPAddress"],
            temporal["NetworkSettings"]["Networks"][network]["IPAddress"],
            entrypoint="python",
        )
        wait_for(lambda: "relay-ready" in docker("logs", relay), "容器回环转发就绪")
        binding = inspect_container(relay)["NetworkSettings"]["Ports"]["8080/tcp"][0]
        origin = f"http://127.0.0.1:{binding['HostPort']}"
        test_password = (
            getpass.getpass("设置本次镜像验收临时密码（输入隐藏）：")
            if interactive
            else secrets.token_urlsafe(24)
        )
        if not test_password:
            raise ValueError("临时密码不能为空")
        environment = {
            "APP_ENV": "test",
            "CONNECTOR_MODE": "fake",
            "LLM_MODE": "fake",
            "DATABASE_URL": URL.create(
                "postgresql+asyncpg",
                username=user,
                password=password,
                host="127.0.0.1",
                port=5432,
                database=database,
            ).render_as_string(hide_password=False),
            "TEMPORAL_CONFIG": json.dumps(
                {"address": "127.0.0.1:7233", "namespace": "default", "task_queue": queue}
            ),
            "AUTH_CONFIG": json.dumps(
                {
                    "username": "image-check",
                    "password_hash": hash_password(test_password),
                    "session_secret": secrets.token_hex(32),
                    "public_origin": origin,
                }
            ),
            "DISCOVERY_CONFIG": json.dumps({"schedule_id": schedule, "interval_seconds": 86400}),
            "SCHEDULING_CONFIG": '{"enabled":false}',
            "DETECTION_CONFIG": '{"enabled":false}',
            "AUTOMATION_CONFIG": '{"enabled":false}',
            "IMAGE_TEST_ORIGIN": origin,
            "IMAGE_TEST_PASSWORD": test_password,
        }
        # migrations 是显式的一次性命令；api/worker 启动不自动修改数据库。
        args = ["run", "--rm", "--network", "container:" + relay, "--entrypoint", "python"]
        for key in environment:
            args += ["--env", key]
        docker(
            *args,
            BACKEND,
            "-m",
            "alembic",
            "upgrade",
            "head",
            environment=dict(os.environ, **environment),
        )
        print("通过：镜像内 Alembic 升级到现有 head", flush=True)
        api = run(prefix + "-api", BACKEND, "api", env=environment, namespace="container:" + relay)

        def api_ready() -> bool:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    api,
                    "python",
                    "-c",
                    "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2))['status']=='ok'",
                ],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0

        wait_for(api_ready, "api /health HTTP 200、status=ok")
        docker(
            "exec",
            api,
            "python",
            "-c",
            "import importlib.util,os; assert os.getuid()!=0; "
            "assert importlib.util.find_spec('pytest') is None; "
            "assert importlib.util.find_spec('ruff') is None",
        )
        frontend = run(
            prefix + "-frontend",
            FRONTEND,
            env={"API_UPSTREAM": "127.0.0.1:8000"},
            namespace="container:" + relay,
        )
        wait_for(
            lambda: (
                inspect_container(frontend)["State"].get("Health", {}).get("Status") == "healthy"
            ),
            "前端镜像 healthy",
        )
        docker("exec", "-i", api, "python", "-", data=HTTP_CHECK)
        print("通过：SPA 深链接、资源 404、匿名 401、登录 200、CSRF 403、退出 204/401", flush=True)
        schedule_attempted = True
        worker = run(
            prefix + "-worker", BACKEND, "worker", env=environment, namespace="container:" + relay
        )

        def worker_ready() -> bool:
            if not inspect_container(worker)["State"]["Running"]:
                raise RuntimeError("worker 镜像提前退出")
            result: dict[str, Any] = json.loads(
                docker(
                    "exec",
                    admin["Id"],
                    "temporal",
                    "task-queue",
                    "describe",
                    "--legacy-mode",
                    "--address",
                    "temporal:7233",
                    "--namespace",
                    "default",
                    "--task-queue",
                    queue,
                    "--output",
                    "json",
                )
            )
            return bool(result.get("pollers"))

        wait_for(worker_ready, "同一后端镜像 worker 已连接 Temporal 并轮询独立队列")
        # 运行入口和 nginx 模板均必须拒绝不合法配置。
        negative = [
            ([BACKEND, "invalid"], "入口"),
            ([BACKEND, "api"], "APP_ENV"),
            (["--env", "API_UPSTREAM=bad/path", FRONTEND], "API_UPSTREAM"),
            (["--env", "API_UPSTREAM=api:8000\nserver", FRONTEND], "API_UPSTREAM"),
        ]
        for args, marker in negative:
            result = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", *args], cwd=ROOT, capture_output=True
            )
            if result.returncode == 0 or marker.encode() not in result.stdout + result.stderr:
                raise AssertionError(f"无效配置未正确失败：{marker}")
        print("通过：无效入口、缺 APP_ENV 与非法代理配置拒绝启动", flush=True)
        for image in (BACKEND, FRONTEND):
            config = json.loads(docker("image", "inspect", image))[0]["Config"]
            if config["User"] in {"", "0", "root", "0:0"}:
                raise AssertionError("镜像未使用非 root 身份")
        report = {
            "checked_at": datetime.now(UTC).isoformat(),
            "status": "passed",
            "api_health": 200,
            "worker_polling": True,
            "frontend_login": True,
            "negative_checks": len(negative),
            "backend_image": json.loads(docker("image", "inspect", BACKEND))[0]["Id"],
            "frontend_image": json.loads(docker("image", "inspect", FRONTEND))[0]["Id"],
        }
        if interactive:
            print(f"浏览地址：{origin}；登录名：image-check", flush=True)
            input("自行登录、刷新详情与查看控制台后，按 Enter 清理本次验收环境：")
        return report
    finally:
        cleanup_errors: list[str] = []
        for name in reversed(containers):
            try:
                docker("rm", "--force", name)
            except RuntimeError:
                cleanup_errors.append(f"容器 {name}")
        # 本次唯一随机 Schedule；不改动个人默认 Schedule。
        deleted = subprocess.run(
            [
                "docker",
                "exec",
                admin["Id"],
                "temporal",
                "schedule",
                "delete",
                "--address",
                "temporal:7233",
                "--namespace",
                "default",
                "--schedule-id",
                schedule,
            ],
            capture_output=True,
        )
        if (
            schedule_attempted
            and deleted.returncode
            and b"not found" not in deleted.stderr.lower()
            and b"notfound" not in deleted.stderr.lower()
        ):
            cleanup_errors.append(f"Schedule {schedule}")
        try:
            sql(f'DROP DATABASE "{database}" WITH (FORCE);')
        except RuntimeError:
            cleanup_errors.append(f"临时库 {database}")
        if cleanup_errors:
            error = RuntimeError("清理失败：" + ", ".join(cleanup_errors))
            current = sys.exception()
            if current is None:
                raise error
            current.add_note(str(error))
        else:
            print("本次镜像容器、Schedule 与临时数据库已清理", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 56 镜像构建与实际启动验收")
    parser.add_argument("--skip-build", action="store_true", help="只验收已经构建的固定本机标签")
    parser.add_argument("--interactive", action="store_true")
    options = parser.parse_args()
    if not options.skip_build:
        build_images()
    report = check_images(interactive=options.interactive)
    destination = ROOT / ".cache" / "images" / uuid4().hex
    destination.mkdir(parents=True)
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"镜像验收全部通过；脱敏报告：{destination / 'report.json'}", flush=True)


if __name__ == "__main__":
    main()
