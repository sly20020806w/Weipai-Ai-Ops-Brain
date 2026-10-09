"""在一次性 CI runner 执行同一统一检查与镜像验收，结束后清理本机依赖。"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    context = subprocess.run(["docker", "context", "inspect"], check=True, capture_output=True)
    endpoint = (
        os.environ.get("DOCKER_HOST")
        or json.loads(context.stdout)[0]["Endpoints"]["docker"]["Host"]
    )
    if not endpoint.startswith(("npipe://", "unix://")):
        raise ValueError("CI 验收只允许本机 Docker socket")
    # 避免在个人环境替换已有数据库密码或移除已有容器。
    existing = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=weipai-ai-ops-brain-local",
        ],
        check=True,
        capture_output=True,
    ).stdout.strip()
    if existing:
        raise ValueError(
            "ci.py 仅用于没有本项目现有容器的一次性 CI runner；"
            "个人验收请使用 check.ps1/check-images.ps1"
        )
    for resource in ("volume", "network"):
        found = subprocess.run(
            [
                "docker",
                resource,
                "ls",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=weipai-ai-ops-brain-local",
            ],
            check=True,
            capture_output=True,
        ).stdout.strip()
        if found:
            raise ValueError("CI runner 存在本项目旧卷或网络；拒绝覆盖或删除")
    environment = dict(os.environ)
    for field in Settings.model_fields.values():
        if isinstance(field.validation_alias, str):
            environment.pop(field.validation_alias, None)
    environment.update(
        {
            "POSTGRES_USER": "weipai",
            "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
            "POSTGRES_PORT": "5432",
            "TEMPORAL_PORT": "7233",
            "TEMPORAL_UI_PORT": "8080",
            "APP_ENV": "test",
            "CONNECTOR_MODE": "fake",
            "LLM_MODE": "fake",
            "TEST_TEMPORAL_ADDRESS": "127.0.0.1:7233",
            "TEST_TEMPORAL_NAMESPACE": "default",
            "TEMPORAL_CONFIG": json.dumps(
                {
                    "address": "127.0.0.1:7233",
                    "namespace": "default",
                    "task_queue": "weipai-ai-tasks",
                }
            ),
        }
    )
    compose = [
        "docker",
        "compose",
        "--env-file",
        os.devnull,
        "--file",
        str(ROOT / "deploy/docker-compose.yml"),
    ]
    try:
        subprocess.run(
            [*compose, "up", "--detach", "--wait", "--wait-timeout", "240"],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/check.py")], cwd=ROOT, env=environment, check=True
        )
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_images.py")],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        print("CI 统一检查与镜像验收全部通过", flush=True)
    finally:
        subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    main()
