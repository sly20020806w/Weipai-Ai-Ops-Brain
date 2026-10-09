"""静态导入边界检查：外部系统 SDK/API 客户端只允许在 connectors/。"""

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# 非 Connector 可使用的基础框架依赖。新基础依赖须显式加入此清单。
FRAMEWORK_MODULES = frozenset(
    {
        "app",
        "alembic",
        "asyncpg",
        "fastapi",
        "pgvector",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "sqlalchemy",
        "starlette",
        "temporalio",
        "uvicorn",
    }
)
# 公司 AI 网关是已有独立客户端；不允许把例外扩展到 tools/agent 其他文件。
GATEWAY_HTTP_FILES = frozenset({"app/agent/client.py", "app/agent/fake.py"})
OUTBOUND_STDLIB = ("socket", "http.client", "urllib.request", "xmlrpc.client")


@dataclass(frozen=True)
class ImportViolation:
    path: Path
    line: int
    module: str
    reason: str


def inspect_imports(source: str, relative_path: Path) -> list[ImportViolation]:
    """只解析源码，不 import SDK；同时检查函数内、TYPE_CHECKING 和静态动态导入。"""
    path = relative_path.as_posix()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        return [ImportViolation(relative_path, error.lineno or 1, "<syntax>", "源码无法解析")]
    if path.startswith("app/connectors/"):
        return []
    violations: list[ImportViolation] = []
    dynamic_aliases = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins"}:
            for alias in node.names:
                if alias.name in {"import_module", "__import__"}:
                    dynamic_aliases.add(alias.asname or alias.name)

    def check(module: str, line: int) -> None:
        root = module.split(".")[0]
        outbound = any(module == name or module.startswith(name + ".") for name in OUTBOUND_STDLIB)
        allowed = root in FRAMEWORK_MODULES or root in sys.stdlib_module_names
        if root == "httpx2" and path in GATEWAY_HTTP_FILES:
            allowed = True
        if outbound or not allowed:
            violations.append(
                ImportViolation(
                    relative_path, line, module, "外部 SDK/API 客户端只能在 connectors/ 引入"
                )
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                check(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            check(node.module, node.lineno)
            if node.module in {"urllib", "http", "xmlrpc"}:
                for alias in node.names:
                    check(f"{node.module}.{alias.name}", node.lineno)
        elif isinstance(node, ast.Call):
            dynamic = (isinstance(node.func, ast.Name) and node.func.id in dynamic_aliases) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"import_module", "__import__"}
            )
            if not dynamic:
                continue
            argument = (
                node.args[0]
                if node.args
                else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "name"), None
                )
            )
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                check(argument.value, node.lineno)
            else:
                violations.append(
                    ImportViolation(
                        relative_path, node.lineno, "<dynamic>", "边界外禁止不可静态确定的动态导入"
                    )
                )
    return sorted(set(violations), key=lambda item: (item.line, item.module))


def find_import_violations(root: Path) -> list[ImportViolation]:
    backend = root / "backend"
    result: list[ImportViolation] = []
    for directory in (backend / "app", backend / "alembic"):
        for file in sorted(directory.rglob("*.py")):
            result.extend(
                inspect_imports(file.read_text(encoding="utf-8-sig"), file.relative_to(backend))
            )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Connector 外部 SDK 导入边界检查")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    root = parser.parse_args().root.resolve()
    if not (root / "backend" / "app").is_dir():
        parser.error("--root 必须指向包含 backend/app 的项目根目录")
    violations = find_import_violations(root)
    for violation in violations:
        print(
            f"{violation.path.as_posix()}:{violation.line}: {violation.module}: {violation.reason}"
        )
    if violations:
        raise SystemExit(1)
    print("Connector 导入边界检查通过", flush=True)


if __name__ == "__main__":
    main()
