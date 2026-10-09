"""Linux CI 扫描器必须拒绝损坏下载/缓存与未验证平台；测试不联网。"""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ci_security_tools", ROOT / "scripts/security_tools.py"
)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_corrupted_cache_is_rejected_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = tmp_path / "tool"
    cached.write_bytes(b"corrupted")
    monkeypatch.setattr(
        installer.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("不允许联网")
    )
    with pytest.raises(RuntimeError, match="缓存 SHA256"):
        installer.verified_download(
            "https://example.invalid/tool", cached, hashlib.sha256(b"official").hexdigest()
        )


def test_validated_cache_is_reused_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = tmp_path / "tool"
    cached.write_bytes(b"official")
    monkeypatch.setattr(
        installer.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("不允许联网")
    )
    installer.verified_download(
        "https://example.invalid/tool", cached, hashlib.sha256(b"official").hexdigest()
    )


def test_unverified_platform_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ValueError, match="经过校验"):
        installer.prepare_tools(tmp_path)
