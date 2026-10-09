"""导入边界的成功/拒绝路径，并在隔离项目中验证统一入口真正失败。"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.connectors.boundaries import find_import_violations, inspect_imports


@pytest.mark.parametrize(
    "source",
    [
        "import kubernetes",
        "from kubernetes.client import ApiClient",
        "import boto3 as cloud",
        "from aliyunsdkcore.client import AcsClient",
        "import alibabacloud_ecs20140526",
        "import prometheus_api_client",
        "import gitlab",
        "import github",
        "import jenkins",
        "import requests",
        "import httpx2",
        "import httpx",
        "import aiohttp",
        "import unknown_future_sdk",
        "if False:\n    import kubernetes",
        "def query():\n    import requests",
        "__import__('kubernetes')",
        "import importlib as il\nil.import_module('kubernetes')",
        "from importlib import import_module as load\nload('kubernetes')",
        "from builtins import __import__ as load\nload('kubernetes')",
        "import importlib\nimportlib.import_module(name='kubernetes')",
        "__import__(variable)",
        "import importlib\nimportlib.import_module(variable)",
        "import socket",
        "import urllib.request",
        "from urllib import request",
        "from http import client",
        "from xmlrpc.client import ServerProxy",
    ],
)
def test_external_imports_rejected_in_tools(source: str) -> None:
    violations = inspect_imports(source, Path("app/tools/bad.py"))
    assert violations
    assert all(item.line > 0 for item in violations)
    assert inspect_imports(source, Path("app/connectors/vendor.py")) == []


@pytest.mark.parametrize(
    "path",
    [
        "app/agent/loop.py",
        "app/api/main.py",
        "app/tasks/service.py",
        "app/connectors_extra/adapter.py",
        "alembic/versions/0005_sample.py",
    ],
)
def test_directory_boundary_is_exact(path: str) -> None:
    assert inspect_imports("import kubernetes", Path(path))


def test_framework_and_gateway_allowlist_are_narrow() -> None:
    source = (
        "from typing import Protocol\n"
        "from app.connectors.base import ReadOnlyConnector\n"
        "import sqlalchemy\nimport temporalio"
    )
    assert inspect_imports(source, Path("app/tools/good.py")) == []
    for path in ["app/agent/client.py", "app/agent/fake.py"]:
        assert inspect_imports("import httpx2", Path(path)) == []
        assert inspect_imports("import kubernetes", Path(path))
    assert inspect_imports("import httpx2", Path("app/agent/loop.py"))
    assert inspect_imports("from .models import Thing", Path("app/tools/good.py")) == []


def test_project_currently_satisfies_import_boundary() -> None:
    assert find_import_violations(Path(__file__).resolve().parents[2]) == []


def test_syntax_errors_do_not_silently_pass() -> None:
    assert inspect_imports("import (", Path("app/tools/bad.py"))[0].module == "<syntax>"


def test_unified_check_fails_for_injected_sdk_and_passes_after_removal(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    tools = tmp_path / "backend" / "app" / "tools"
    tools.mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    check_script = tmp_path / "scripts" / "check.py"
    check_script.write_text(
        (root / "scripts" / "check.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    bad = tools / "boundary_probe.py"
    bad.write_text("import kubernetes\n", encoding="utf-8")
    # 实际调用同一个统一入口，必须在 ruff/mypy/pytest 之前由边界门禁拦截。
    result = subprocess.run(
        [sys.executable, str(check_script)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode != 0
    assert "app/tools/boundary_probe.py:1: kubernetes" in result.stdout
    assert "ruff" not in result.stdout
    bad.unlink()
    clean = subprocess.run(
        [sys.executable, "-m", "app.connectors.boundaries", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
