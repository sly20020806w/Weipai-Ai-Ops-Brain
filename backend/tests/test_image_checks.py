"""启动探测的瞬时超时可重试；持续超时和实际错误不能成为成功。"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "image_checks", Path(__file__).resolve().parents[2] / "scripts/check_images.py"
)
assert spec is not None and spec.loader is not None
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def test_startup_timeout_retries_until_real_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    clock = iter((0, 1, 2, 3))
    monkeypatch.setattr(checks.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(checks.time, "sleep", lambda _: None)

    def probe() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(["docker", "exec"], 10)
        return calls == 3

    checks.wait_for(probe, "API 健康检查")
    assert calls == 3


def test_persistent_timeout_preserves_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter((0, 1, 91))
    monkeypatch.setattr(checks.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(checks.time, "sleep", lambda _: None)

    def probe() -> bool:
        raise subprocess.TimeoutExpired(["docker", "exec"], 10)

    with pytest.raises(RuntimeError, match="验收等待超时"):
        checks.wait_for(probe, "API 健康检查")


def test_actual_startup_failure_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks.time, "monotonic", lambda: 0)

    def probe() -> bool:
        raise RuntimeError("Worker 已退出")

    with pytest.raises(RuntimeError, match="Worker 已退出"):
        checks.wait_for(probe, "Worker 启动")
