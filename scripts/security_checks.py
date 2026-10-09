"""安全验收工具；不读取应用凭证，不连接公司运维系统。"""

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from security_tools import prepare_tools

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "deploy" / "security"


def render_policy(filename: str, replacements: dict[str, str]) -> dict[str, Any]:
    """只替换解析后的 JSON 字符串，避免插值把权限或 JSON 结构改写。"""
    document: dict[str, Any] = json.loads((POLICIES / filename).read_text(encoding="utf-8"))

    def replace(value: Any) -> Any:
        if isinstance(value, str):

            def substitute(match: re.Match[str]) -> str:
                replacement = replacements[match[1]]
                if not replacement or any(ord(char) < 32 for char in replacement):
                    raise ValueError("策略占位符必须是非空文本")
                return replacement

            return re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", substitute, value)
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value

    result: dict[str, Any] = replace(document)
    return result


def expected_packages(root: Path) -> set[tuple[str, str, str]]:
    lock = tomllib.loads((root / "backend" / "uv.lock").read_text(encoding="utf-8"))
    expected = {("PyPI", item["name"], item["version"]) for item in lock["package"]}
    # pnpm v9 的 packages 块只含唯一包坐标，不包含 snapshots 的 peer 后缀。
    text = (root / "frontend" / "pnpm-lock.yaml").read_text(encoding="utf-8")
    if not text.startswith("lockfileVersion: '9.0'"):
        raise ValueError("pnpm 锁格式变更，必须更新完整覆盖校验")
    section = text.split("\npackages:\n", 1)[1].split("\nsnapshots:\n", 1)[0]
    for coordinate in re.findall(r"^  (\S+):$", section, flags=re.MULTILINE):
        name, version = coordinate.strip("'").rsplit("@", 1)
        if not re.fullmatch(r"\d[^()]*", version):
            raise ValueError("无法识别 pnpm 包版本，禁止漏扫")
        expected.add(("npm", name, version))
    if not any(item[0] == "PyPI" for item in expected) or not any(
        item[0] == "npm" for item in expected
    ):
        raise ValueError("两个锁文件均须包含依赖")
    return expected


def verify_osv_report(root: Path, report: dict[str, Any]) -> dict[str, int]:
    expected = expected_packages(root)
    actual: set[tuple[str, str, str]] = set()
    vulnerabilities: set[str] = set()
    for result in report["results"]:
        source = Path(result["source"]["path"]).resolve()
        if source not in {root / "backend" / "uv.lock", root / "frontend" / "pnpm-lock.yaml"}:
            raise ValueError("扫描报告包含意外来源")
        for item in result["packages"]:
            package = item["package"]
            actual.add((package["ecosystem"], package["name"], package["version"]))
            vulnerabilities.update(vuln["id"] for vuln in item.get("vulnerabilities", []))
    if actual != expected:
        raise ValueError(f"依赖扫描覆盖不完整：期望 {len(expected)}，实际 {len(actual)}")
    if vulnerabilities:
        # 不忽略低分或未知严重度。任何已知漏洞均使门禁失败。
        raise ValueError("存在依赖漏洞：" + ", ".join(sorted(vulnerabilities)))
    return {
        "python_packages": sum(item[0] == "PyPI" for item in actual),
        "npm_packages": sum(item[0] == "npm" for item in actual),
        "vulnerabilities": 0,
    }


def run(command: list[str], *, data: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=ROOT, input=data, capture_output=True, text=True, encoding="utf-8", timeout=180
    )


def require_complete_secret_scan(result: subprocess.CompletedProcess[str]) -> None:
    # Gitleaks 遇到部分文件读取失败仍可能退出 0，不能据此声明零命中。
    output = (result.stdout + result.stderr).lower()
    if any(
        message in output
        for message in ("could not read file", "cannot allocate memory", "permission denied")
    ):
        raise RuntimeError("密钥扫描存在文件读取错误；扫描范围不完整，不能标记为通过")


def scan(output: Path) -> dict[str, Any]:
    leaks, osv = prepare_tools(ROOT)
    # 阳性对照只经 stdin；随机合成 token 不落盘，也不用于外部请求。
    synthetic = "ghp_" + secrets.token_hex(20)
    probe = output / "scanner-positive-control.json"
    checked = run(
        [
            str(leaks),
            "stdin",
            "--config",
            str(ROOT / ".gitleaks.toml"),
            "--redact=100",
            "--no-banner",
            "--no-color",
            "--ignore-gitleaks-allow",
            "--gitleaks-ignore-path",
            str(output / "no-ignore-file"),
            "--report-format",
            "json",
            "--report-path",
            str(probe),
        ],
        data="github_token = " + synthetic,
    )
    if checked.returncode != 1 or not probe.is_file():
        raise RuntimeError("密钥扫描阳性对照失败，扫描器未证明能检出 token")
    probe_text = probe.read_text(encoding="utf-8")
    if not json.loads(probe_text) or synthetic in probe_text:
        raise RuntimeError("密钥扫描阳性对照未检出或报告未完全脱敏")
    secret_count = 0
    for mode in ("dir", "git"):
        if mode == "git" and run(["git", "rev-parse", "--verify", "HEAD"]).returncode:
            continue
        path = output / f"secrets-{mode}.json"
        path.unlink(missing_ok=True)
        result = run(
            [
                str(leaks),
                mode,
                str(ROOT),
                "--config",
                str(ROOT / ".gitleaks.toml"),
                "--redact=100",
                "--no-banner",
                "--no-color",
                "--ignore-gitleaks-allow",
                "--gitleaks-ignore-path",
                str(output / "no-ignore-file"),
                "--report-format",
                "json",
                "--report-path",
                str(path),
            ]
        )
        if result.returncode not in {0, 1} or not path.is_file():
            raise RuntimeError("密钥扫描未成功执行；禁止把工具错误算作零命中")
        require_complete_secret_scan(result)
        findings = json.loads(path.read_text(encoding="utf-8"))
        secret_count += len(findings)
        if result.returncode or findings:
            # 报告由 Gitleaks 完整脱敏，终端仅打印路径和规则。
            locations = [
                f"{item['File']}:{item['StartLine']} ({item['RuleID']})" for item in findings
            ]
            raise ValueError("密钥扫描命中：" + ", ".join(locations))
    osv_path = output / "dependencies.json"
    osv_path.unlink(missing_ok=True)
    result = run(
        [
            str(osv),
            "scan",
            "source",
            "--lockfile",
            str(ROOT / "backend" / "uv.lock"),
            "--lockfile",
            str(ROOT / "frontend" / "pnpm-lock.yaml"),
            "--no-resolve",
            "--all-packages",
            "--format",
            "json",
            "--output-file",
            str(osv_path),
        ]
    )
    if result.returncode not in {0, 1} or not osv_path.is_file():
        raise RuntimeError("公共漏洞库扫描失败；需要联网重试，禁止使用旧报告代替")
    summary: dict[str, Any] = verify_osv_report(
        ROOT, json.loads(osv_path.read_text(encoding="utf-8"))
    )
    if result.returncode:
        raise RuntimeError("OSV 返回非零状态，门禁未通过")
    summary.update(
        {
            "secret_findings": secret_count,
            "scanner_positive_control": "passed",
            "git_history_scanned": run(["git", "rev-parse", "--verify", "HEAD"]).returncode == 0,
            "gitleaks_version": "8.30.1",
            "osv_version": "2.6.0",
            "lock_sha256": {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in ("backend/uv.lock", "frontend/pnpm-lock.yaml")
            },
        }
    )
    return summary


def check_local_rbac(node: str) -> dict[str, int]:
    """只在本机 kind 容器的随机命名空间验收；不使用宿主 kubeconfig。"""
    context = run(["docker", "context", "inspect"])
    host = json.loads(context.stdout)[0]["Endpoints"]["docker"]["Host"]
    if context.returncode or not host.startswith(("npipe://", "unix://")):
        raise ValueError("RBAC 验收只允许本机 Docker socket")
    inspected = run(["docker", "inspect", node])
    if inspected.returncode:
        raise ValueError("本机 kind 节点不存在，请按安全验收文档准备")
    config = json.loads(inspected.stdout)[0]["Config"]
    if config["Labels"].get("io.x-k8s.kind.role") != "control-plane":
        raise ValueError("仅允许经过 Docker kind 标签核对的本机 control-plane")

    def kubectl(*arguments: str, data: str | None = None) -> subprocess.CompletedProcess[str]:
        return run(["docker", "exec", "-i", node, "kubectl", *arguments], data=data)

    # 任何集群写请求前验证容器内部 kubeconfig，标签本身不能证明目标仍是本机。
    cluster_response = kubectl("config", "view", "--minify", "-o", "json")
    if cluster_response.returncode:
        raise RuntimeError("无法核对本机 kind API 地址")
    cluster = json.loads(cluster_response.stdout)["clusters"][0]["cluster"]
    if urlsplit(cluster["server"]).hostname not in {"127.0.0.1", "localhost", node}:
        raise ValueError("kind API Server 地址必须仍在本机节点内")

    prefix = "security-" + uuid4().hex[:12]
    platform, target = prefix + "-platform", prefix + "-target"
    manifest = render_policy(
        "kubernetes.json",
        {
            "PLATFORM_NAMESPACE": platform,
            "TARGET_NAMESPACE": target,
            "BINDING_PREFIX": prefix,
        },
    )
    reader = f"system:serviceaccount:{platform}:ai-reader"
    executor = f"system:serviceaccount:{platform}:ai-executor"
    checked = 0
    created_namespaces: list[str] = []
    try:
        for namespace in (platform, target):
            result = kubectl("create", "namespace", namespace)
            if result.returncode:
                raise RuntimeError("无法创建隔离验收命名空间")
            created_namespaces.append(namespace)
        result = kubectl("create", "-f", "-", data=json.dumps(manifest))
        if result.returncode:
            raise RuntimeError("RBAC 清单被本机 API Server 拒绝：" + result.stderr)
        for account in ("ai-reader", "ai-executor"):
            result = kubectl("get", "sa", account, "-n", platform, "-o", "json")
            if result.returncode or json.loads(result.stdout)["automountServiceAccountToken"]:
                raise AssertionError("ServiceAccount 自动挂载未关闭")
        for resource in ("deployments.apps", "pods", "events"):
            result = kubectl("get", resource, "-n", target, "--as", reader, "-o", "json")
            if result.returncode:
                raise AssertionError("Reader 在授权命名空间无法读取")
        # 每种写动词、敏感资源、提权入口都请求真实 SubjectAccessReview。
        reviews: list[dict[str, Any]] = []

        def review(identity: str, verb: str, resource: str) -> None:
            base, _, subresource = resource.partition("/")
            name, _, group = base.partition(".")
            reviews.append(
                {
                    "apiVersion": "authorization.k8s.io/v1",
                    "kind": "SubjectAccessReview",
                    "spec": {
                        "user": identity,
                        "groups": [
                            "system:authenticated",
                            "system:serviceaccounts",
                            f"system:serviceaccounts:{platform}",
                        ],
                        "resourceAttributes": {
                            "namespace": target,
                            "verb": verb,
                            "group": group,
                            "resource": name,
                            "subresource": subresource,
                        },
                    },
                }
            )

        for identity in (reader, executor):
            for verb in ("create", "update", "patch", "delete", "deletecollection"):
                for resource in (
                    "deployments.apps",
                    "deployments.apps/scale",
                    "pods",
                    "secrets",
                    "rolebindings.rbac.authorization.k8s.io",
                ):
                    review(identity, verb, resource)
        for verb, resource in (
            ("get", "secrets"),
            ("create", "pods/exec"),
            ("create", "serviceaccounts/token"),
            ("escalate", "roles.rbac.authorization.k8s.io"),
            ("bind", "clusterroles.rbac.authorization.k8s.io"),
            ("impersonate", "serviceaccounts"),
        ):
            review(reader, verb, resource)
        reviewed = kubectl(
            "create",
            "-f",
            "-",
            "-o",
            "json",
            data=json.dumps({"apiVersion": "v1", "kind": "List", "items": reviews}),
        )
        if reviewed.returncode:
            raise RuntimeError("实际 SubjectAccessReview 请求失败：" + reviewed.stderr)
        # kubectl create 对 List 逐个输出 JSON 对象，而非一个聚合 List。
        decisions: list[dict[str, Any]] = []
        remainder = reviewed.stdout.strip()
        decoder = json.JSONDecoder()
        while remainder:
            item, offset = decoder.raw_decode(remainder)
            decisions.append(item)
            remainder = remainder[offset:].lstrip()
        if len(decisions) != len(reviews) or any(
            item["status"].get("allowed") is not False or item["status"].get("evaluationError")
            for item in decisions
        ):
            raise AssertionError("身份意外获得写入/提权权限或授权检查失败")
        checked = len(decisions)
        # 再用短时 Reader token 真正认证，凭证只在内存和 stdin，绝不写文件/日志。
        reader_token = kubectl("create", "token", "ai-reader", "-n", platform, "--duration=10m")
        ca = kubectl(
            "config",
            "view",
            "--minify",
            "--raw",
            "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority-data}",
        )
        if reader_token.returncode or ca.returncode:
            raise RuntimeError("无法准备本机 Reader 临时认证")
        reader_config = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "reader",
                "clusters": [
                    {
                        "name": "local",
                        "cluster": {
                            "server": cluster["server"],
                            "certificate-authority-data": ca.stdout.strip(),
                        },
                    }
                ],
                "users": [{"name": "reader", "user": {"token": reader_token.stdout.strip()}}],
                "contexts": [
                    {
                        "name": "reader",
                        "context": {"cluster": "local", "user": "reader", "namespace": target},
                    }
                ],
            }
        )
        who = kubectl("--kubeconfig=/dev/stdin", "auth", "whoami", "-o", "json", data=reader_config)
        if who.returncode or json.loads(who.stdout)["status"]["userInfo"]["username"] != reader:
            raise AssertionError("写入拒绝验收没有使用真实 Reader 身份")
        # 真实写请求必须返回 Forbidden，不能只靠本地规则模拟或 404。
        denied = kubectl(
            "--kubeconfig=/dev/stdin",
            "create",
            "configmap",
            "reader-write-probe",
            "-n",
            target,
            "--from-literal=probe=synthetic",
            "--dry-run=server",
            data=reader_config,
        )
        if denied.returncode == 0 or "forbidden" not in denied.stderr.lower():
            diagnostic = denied.stderr.replace(reader_token.stdout.strip(), "REDACTED")
            raise AssertionError("Reader 写请求未得到预期拒绝：" + diagnostic)
        outside = kubectl("get", "pods", "-n", platform, "--as", reader)
        if outside.returncode == 0 or "forbidden" not in outside.stderr.lower():
            raise AssertionError("Reader 能读取授权范围外的工作负载")
        denied = kubectl("get", "pods", "-n", target, "--as", executor)
        if denied.returncode == 0 or "forbidden" not in denied.stderr.lower():
            raise AssertionError("未授权 Executor 不应获得工作负载权限")
        return {
            "rbac_denied_checks": checked + 3,
            "rbac_read_checks": 3,
            "reader_token_identity_checks": 1,
        }
    finally:
        failures = []
        for kind in ("clusterrolebinding", "clusterrole"):
            result = kubectl(
                "delete", kind, prefix + "-namespace-reader", "--ignore-not-found=true"
            )
            if result.returncode:
                failures.append(kind)
        for namespace in created_namespaces:
            result = kubectl(
                "delete",
                "namespace",
                namespace,
                "--ignore-not-found=true",
                "--wait=true",
                "--timeout=60s",
            )
            if result.returncode:
                failures.append(namespace)
        if failures:
            raise RuntimeError("隔离 RBAC 对象清理失败：" + ", ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 55 安全扫描与本机 RBAC 验收")
    parser.add_argument("--local-kind-node", help="只允许本机 Docker kind control-plane")
    options = parser.parse_args()
    output = ROOT / ".cache" / "security" / uuid4().hex
    output.mkdir(parents=True)
    summary = scan(output)
    if options.local_kind_node:
        summary.update(check_local_rbac(options.local_kind_node))
    summary["checked_at"] = datetime.now(UTC).isoformat()
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    print(f"安全扫描通过；脱敏报告：{output}", flush=True)


if __name__ == "__main__":
    main()
