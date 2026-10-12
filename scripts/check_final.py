"""Step 58 本机最终验收编排；复用现有检查，不部署或启用真实写操作。"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.db.base import utc_now
from app.learning.evaluation.acceptance import AcceptanceCriteria

ROOT = Path(__file__).resolve().parents[1]


def source_manifest() -> dict[str, str]:
    names = (
        subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    files = {}
    for name in sorted(set(names) - {""}):
        path = ROOT / name
        if path.is_file():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def manifest_fingerprint(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(value))
    return digest.hexdigest()


def source_fingerprint() -> str:
    return manifest_fingerprint(source_manifest())


def resume_checks(
    path: Path, current: dict[str, str], criteria: AcceptanceCriteria, node: str
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    path = path.resolve()
    area = (ROOT / ".cache/acceptance").resolve()
    if not path.is_relative_to(area) or path.name != "report.json":
        raise ValueError("续跑只能使用本项目独立运行的 report.json")
    prior = json.loads(path.read_text(encoding="utf-8"))
    old = prior.get("source_files")
    if not isinstance(old, dict) or manifest_fingerprint(old) != prior.get("source_sha256"):
        raise ValueError("缺少或损坏源码快照，必须执行完整验收")
    if (
        prior.get("scope") != "local_fake"
        or prior.get("criteria") != criteria.model_dump(mode="json")
        or prior.get("local_kind_node", "weipai-sim-control-plane") != node
        or prior.get("production_writes_enabled") is not False
    ):
        raise ValueError("验收范围或条件变化，不能复用结果")
    changed = sorted(
        name for name in old.keys() | current.keys() if old.get(name) != current.get(name)
    )
    allowed = {
        "scripts/check_final.py",
        "check-final.ps1",
        "backend/tests/test_final_checks.py",
        "docs/final-acceptance.md",
    }
    if any(name not in allowed and not name.startswith("frontend/smoke/") for name in changed):
        raise ValueError("业务源码或依赖已变更，必须重新执行完整验收")
    reusable = {
        "database",
        "workflow",
        "schedules",
        "replay",
        "security",
        "readonly",
        "shadow",
        "images",
    }
    entries = {}
    for entry in prior.get("stages", []):
        name = entry.get("name")
        if name not in reusable or entry.get("status") != "passed" or entry.get("exit_code") != 0:
            continue
        log = Path(entry["log"]).resolve()
        if not log.is_relative_to(area) or not log.is_file():
            raise ValueError("缺少原始验收日志，不能复用")
        if hashlib.sha256(log.read_bytes()).hexdigest() != entry.get("log_sha256"):
            raise ValueError("原始验收日志已变化，不能复用")
        entries[name] = dict(
            entry, reused_from=str(path), validated_source_sha256=prior["source_sha256"]
        )
    return entries, changed


def main() -> None:
    parser = argparse.ArgumentParser(description="本机最终验收；真实接入留至入职后")
    parser.add_argument("--rca-threshold", type=float, default=1.0)
    parser.add_argument("--minimum-samples", type=int, default=3)
    parser.add_argument("--local-kind-node", default="weipai-sim-control-plane")
    parser.add_argument("--resume-from", type=Path)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise ValueError("本机完整最终验收使用 Windows PowerShell 与 Edge；CI 保留原跨平台入口")
    context = json.loads(
        subprocess.run(
            ["docker", "context", "inspect"],
            check=True,
            capture_output=True,
        ).stdout
    )[0]
    endpoint = os.environ.get("DOCKER_HOST") or context["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith(("npipe://", "unix://")):
        raise ValueError("最终验收只允许本机 Docker socket")
    criteria = AcceptanceCriteria(
        rca_hit_rate=args.rca_threshold, minimum_samples=args.minimum_samples
    )
    files = source_manifest()
    reused, changed = (
        resume_checks(args.resume_from, files, criteria, args.local_kind_node)
        if args.resume_from
        else ({}, [])
    )
    # 全部子进程隔离个人接入配置；本机基础设施凭证由既有脚本加载。
    environment = dict(os.environ)
    for field in Settings.model_fields.values():
        if isinstance(field.validation_alias, str):
            environment.pop(field.validation_alias, None)
    environment.update(APP_ENV="test", CONNECTOR_MODE="fake", LLM_MODE="fake")
    run_dir = ROOT / ".cache" / "acceptance" / uuid4().hex
    run_dir.mkdir(parents=True)
    latest = run_dir.parent / "latest.json"
    entries: list[dict[str, object]] = []
    result: dict[str, object] = {
        "scope": "local_fake",
        "status": "running",
        "started_at": utc_now().isoformat(),
        "source_sha256": manifest_fingerprint(files),
        "source_files": files,
        "local_kind_node": args.local_kind_node,
        "resume_from": str(args.resume_from) if args.resume_from else None,
        "validation_changes": changed,
        "report_directory": str(run_dir),
        "criteria": criteria.model_dump(mode="json"),
        "stages": entries,
        "real_environment_verified": False,
        "production_writes_enabled": False,
        "deferred": [
            "真实 ACK 上线与 Discovery",
            "真实告警和公司网关影子调查及本人标注",
            "真实多场景 Replay 基准与本人阈值",
            "首个真实 L1 动作与生产授权效果",
        ],
    }

    def save() -> None:
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        (run_dir / "report.json").write_text(encoded, encoding="utf-8")
        latest.write_text(encoded, encoding="utf-8")

    def powershell(script: str, *arguments: str) -> list[str]:
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / script),
            *arguments,
        ]

    def python(script: str, *arguments: str) -> list[str]:
        return [sys.executable, str(ROOT / "scripts" / script), *arguments]

    stages = [
        ("unified", "统一检查、安全扫描、Fake E2E 与前端构建", powershell("check.ps1")),
        (
            "database",
            "完整数据库/Temporal 场景回归与迁移（零跳过）",
            python("check_db.py", "--full"),
        ),
        ("workflow", "既有 Workflow 兼容与恢复", powershell("check-workflow.ps1")),
        ("schedules", "日历与官方时间跳跃专项", powershell("check-schedules.ps1")),
        ("replay", "Replay 边界、十项手算指标及失败场景", powershell("check-replay.ps1")),
        (
            "security",
            "本机 Reader RBAC、鉴权与扫描",
            powershell("check-security.ps1", "-LocalKindNode", args.local_kind_node),
        ),
        ("readonly", "Helm、本机只读 Discovery 与零写凭证", powershell("check-ack.ps1")),
        (
            "shadow",
            "影子 RCA、独立标签和 Replay 门槛",
            powershell(
                "check-shadow.ps1",
                "-RcaThreshold",
                str(criteria.rca_hit_rate),
                "-MinimumSamples",
                str(criteria.minimum_samples),
            ),
        ),
        ("images", "重建镜像、API/Worker 与前端实际启动", python("check_images.py")),
    ]
    stages += [
        (name, label, powershell(script))
        for name, label, script in (
            ("browser_tasks", "浏览器登录、17 入口与任务证据", "demo-task-pages.ps1"),
            ("browser_approvals", "浏览器审批、判断、信息与接管", "demo-approval-pages.ps1"),
            ("browser_cognition", "浏览器服务图、知识与 Runbook CRUD", "demo-cognition-pages.ps1"),
            ("browser_operations", "浏览器七类运营场景和巡检风险", "demo-operations-pages.ps1"),
            ("browser_dashboard", "浏览器总览、指标与审计筛选", "demo-dashboard-pages.ps1"),
            ("browser_chat", "浏览器 SSE、追问与 Human 任务", "demo-chat-pages.ps1"),
        )
    ]
    save()
    try:
        for name, label, command in stages:
            if name in reused:
                entries.append(reused[name])
                save()
                print(f"复用已通过检查（业务源码不变、原始日志哈希一致）：{label}", flush=True)
                continue
            entry: dict[str, object] = {
                "name": name,
                "label": label,
                "status": "running",
                "started_at": utc_now().isoformat(),
                "log": str(run_dir / f"{name}.log"),
            }
            entries.append(entry)
            save()
            print(f"[{len(entries)}/{len(stages)}] {label}；日志：{entry['log']}", flush=True)
            with (run_dir / f"{name}.log").open("w", encoding="utf-8") as log:
                completed = subprocess.run(
                    command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT
                )
            entry.update(
                exit_code=completed.returncode,
                finished_at=utc_now().isoformat(),
                status="passed" if completed.returncode == 0 else "failed",
                log_sha256=hashlib.sha256((run_dir / f"{name}.log").read_bytes()).hexdigest(),
            )
            save()
            if completed.returncode:
                raise RuntimeError(f"验收失败：{label}；请查看 {entry['log']}")
            print(f"通过：{label}", flush=True)
        shadow = json.loads((ROOT / ".cache/shadow/latest.json").read_text(encoding="utf-8"))
        if shadow["status"] != "passed":
            raise RuntimeError("缺少本次影子验收成功报告")
        result["shadow"] = shadow
        if source_fingerprint() != result["source_sha256"]:
            raise RuntimeError("验收期间源码发生变化，请对最终版本重新运行")
        result["status"] = "passed"
        print("Step 58 本地最终验收全部通过；真实接入与真实生产写操作待入职后验收。", flush=True)
    except BaseException:
        result["status"] = "failed"
        for entry in entries:
            if entry["status"] == "running":
                entry["status"] = "interrupted"
        raise
    finally:
        result["finished_at"] = utc_now().isoformat()
        save()
        print(f"最终验收报告：{latest}", flush=True)


if __name__ == "__main__":
    main()
