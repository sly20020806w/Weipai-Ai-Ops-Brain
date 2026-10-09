"""统一入口在无缓存的检出目录中也能准备 pytest，并检查两个平台。"""

import importlib.util
from pathlib import Path
from typing import Any

import pytest


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_fresh_checkout_prepares_pytest_and_checks_both_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    source = Path(__file__).resolve().parents[2] / "scripts/check.py"
    spec = importlib.util.spec_from_file_location("fresh_checkout_check", source)
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    root = tmp_path / "fresh-checkout"
    monkeypatch.setattr(entry, "__file__", str(root / "scripts/check.py"))
    monkeypatch.setattr(entry.sys, "platform", platform)
    monkeypatch.setattr(entry, "check_git_hygiene", lambda path: None)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> None:
        commands.append(command)
        if command[1:3] == ["-m", "pytest"]:
            base = Path(command[command.index("--basetemp") + 1])
            assert base.parent == root / ".cache"
            assert base.parent.is_dir()

    monkeypatch.setattr(entry.subprocess, "run", run)
    assert not (root / ".cache").exists()
    entry.main()
    type_checks = [command for command in commands if command[1:3] == ["-m", "mypy"]]
    assert [command[command.index("--platform") + 1] for command in type_checks] == [
        "win32",
        "linux",
    ]
    assert sum(command[1:3] == ["-m", "pytest"] for command in commands) == 1
