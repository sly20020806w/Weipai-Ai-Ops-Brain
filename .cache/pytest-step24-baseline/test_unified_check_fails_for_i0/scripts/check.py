"""从仓库根目录调用的统一检查入口。"""

import subprocess
import sys
from pathlib import Path


def check_git_hygiene(root: Path) -> None:
    tracked = (
        subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
        .stdout.decode("utf-8")
        .split("\0")
    )
    forbidden = [
        name
        for name in tracked
        if any(
            part == ".venv" or part == ".env" or part.startswith(".env.")
            for part in Path(name).parts
        )
    ]
    if forbidden:
        raise RuntimeError(f"禁止跟踪本地环境文件：{forbidden}")
    probes = [".env", ".env.local", "backend/.env", "backend/.venv/probe"]
    ignored = (
        subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
            cwd=root,
            input=("\0".join(probes) + "\0").encode("utf-8"),
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .rstrip("\0")
        .split("\0")
    )
    if set(ignored) != set(probes):
        raise RuntimeError(".env 或 .venv 的 Git 忽略规则未生效")
    print("Git 环境文件检查通过", flush=True)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    backend = root / "backend"
    commands = [
        ["app.connectors.boundaries", "--root", str(root)],
        [
            "ruff",
            "check",
            ".",
            "../scripts/check.py",
            "../scripts/check_db.py",
            "../scripts/demo_events.py",
        ],
        [
            "ruff",
            "format",
            "--check",
            ".",
            "../scripts/check.py",
            "../scripts/check_db.py",
            "../scripts/demo_events.py",
        ],
        [
            "mypy",
            "app",
            "tests",
            "alembic",
            "../scripts/check.py",
            "../scripts/check_db.py",
            "../scripts/demo_events.py",
        ],
        ["pytest", "--basetemp", str(root / ".cache" / "pytest-check")],
    ]
    for command in commands:
        print(f"运行：{' '.join(command)}", flush=True)
        subprocess.run([sys.executable, "-m", *command], cwd=backend, check=True)
    check_git_hygiene(root)
    print("统一检查全部通过", flush=True)


if __name__ == "__main__":
    main()
