"""源码和原始日志必须可核验；仅验收驱动改动可续跑，业务变化必须全量重测。"""

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from app.learning.evaluation.acceptance import AcceptanceCriteria

spec = importlib.util.spec_from_file_location(
    "final_checks", Path(__file__).resolve().parents[2] / "scripts/check_final.py"
)
assert spec is not None and spec.loader is not None
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    folder = tmp_path / ".cache/acceptance/test"
    folder.mkdir(parents=True)
    log = folder / "database.log"
    log.write_text("synthetic test evidence", encoding="utf-8")
    files = {"backend/app/example.py": hashlib.sha256(b"business").hexdigest()}
    data: dict[str, Any] = {
        "scope": "local_fake",
        "criteria": AcceptanceCriteria().model_dump(mode="json"),
        "production_writes_enabled": False,
        "source_files": files,
        "source_sha256": checks.manifest_fingerprint(files),
        "stages": [
            {
                "name": "database",
                "status": "passed",
                "exit_code": 0,
                "log": str(log),
                "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            }
        ],
    }
    path = folder / "report.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, files


def test_browser_test_change_can_resume_with_original_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, files = checkpoint(tmp_path, monkeypatch)
    files["frontend/smoke/shell.spec.ts"] = hashlib.sha256(b"test repair").hexdigest()
    reused, changed = checks.resume_checks(
        path, files, AcceptanceCriteria(), "weipai-sim-control-plane"
    )
    assert set(reused) == {"database"} and reused["database"]["reused_from"] == str(path)
    assert changed == ["frontend/smoke/shell.spec.ts"]


def test_business_change_cannot_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, files = checkpoint(tmp_path, monkeypatch)
    files["backend/app/example.py"] = hashlib.sha256(b"changed business").hexdigest()
    with pytest.raises(ValueError, match="业务源码"):
        checks.resume_checks(path, files, AcceptanceCriteria(), "weipai-sim-control-plane")


def test_changed_log_cannot_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, files = checkpoint(tmp_path, monkeypatch)
    (path.parent / "database.log").write_text("altered", encoding="utf-8")
    with pytest.raises(ValueError, match="日志已变化"):
        checks.resume_checks(path, files, AcceptanceCriteria(), "weipai-sim-control-plane")


def test_changed_criteria_cannot_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, files = checkpoint(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="条件变化"):
        checks.resume_checks(
            path, files, AcceptanceCriteria(minimum_samples=4), "weipai-sim-control-plane"
        )
