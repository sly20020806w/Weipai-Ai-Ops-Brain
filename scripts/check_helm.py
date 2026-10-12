"""只在本机渲染 Helm，不连接 Kubernetes，不读取凭证或部署任何资源。"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/weipai-ops"


def helm_path() -> str:
    command = shutil.which("helm")
    local = ROOT / ".tools/helm/windows-amd64/helm.exe"
    if command:
        return command
    if local.is_file():
        return str(local)
    raise RuntimeError("请安装 Helm 3.19+；Windows 可执行 scripts/install-helm.ps1")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_documents(documents: list[dict[str, Any]]) -> None:
    require(bool(documents), "Helm 未渲染任何资源")
    for item in documents:
        kind = item["kind"]
        require(kind != "Secret", "Helm 禁止生成 Secret；只能引用外部已创建的 Secret")
        if kind in {"Role", "ClusterRole"}:
            for rule in item.get("rules", []):
                require(set(rule.get("verbs", [])) <= {"get", "list", "watch"}, "RBAC 含写权限")
                require(
                    set(rule.get("resources", []))
                    <= {"namespaces", "pods", "events", "deployments"},
                    "Reader RBAC 超出已实现 Connector 的只读资源范围",
                )
        if kind in {"RoleBinding", "ClusterRoleBinding"}:
            require(
                all(s["name"].endswith("-reader") for s in item.get("subjects", [])),
                "只读 Chart 不得为其他身份绑定权限",
            )
        if kind == "Service":
            require(item["spec"].get("type", "ClusterIP") == "ClusterIP", "禁止直接公开服务端口")
    applications = [
        d
        for d in documents
        if d["kind"] == "Deployment"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") == "weipai-ops"
    ]
    require(len(applications) == 3, "必须有 api、worker、frontend 三个应用 Deployment")
    for deployment in applications:
        spec = deployment["spec"]["template"]["spec"]
        container = spec["containers"][0]
        name = container["name"]
        require(spec.get("automountServiceAccountToken") is False, "应用禁止自动挂载默认 token")
        require(spec["securityContext"].get("runAsNonRoot") is True, "应用必须使用非 root 身份")
        require(
            container["securityContext"].get("readOnlyRootFilesystem") is True, "根目录必须只读"
        )
        require("@sha256:" in container["image"], "应用镜像必须使用 digest")
        require("envFrom" not in container, "禁止不加选择地加载 Secret 的全部键")
        require(
            deployment["spec"]["selector"]["matchLabels"].get("app.kubernetes.io/part-of")
            == "weipai-ops",
            "应用 selector 必须与 Temporal 隔离",
        )
        env = {entry["name"]: entry for entry in container.get("env", [])}
        require(not any("EXECUTOR" in key or "NOTIFICATION" in key for key in env), "禁止写凭证")
        if name in {"api", "worker"}:
            require(env["OPS_READ_ONLY"]["value"] == "true", "应用必须强制只读")
            require(env["CONNECTOR_MODE"]["value"] == "real", "云端配置不能伪装 Fake 为真实数据")
            require(env["WORKER_MODE"]["value"] == "readonly_discovery", "Worker 模式必须显式配置")
            require(
                json.loads(env["EXECUTION_CONFIG"]["value"]) == {"enabled": False, "bindings": []},
                "Executor 必须关闭",
            )
        if name == "worker":
            require(spec["serviceAccountName"].endswith("-reader"), "Worker 只能使用 Reader")
            projections = [v["projected"] for v in spec["volumes"] if "projected" in v]
            require(len(projections) == 1, "必须只投射一个 Reader 身份卷")
            token = projections[0]["sources"][0]["serviceAccountToken"]
            require(0 < token["expirationSeconds"] <= 3600, "Reader token 必须短时")
        else:
            require(spec["serviceAccountName"].endswith("-console"), "控制台不应携带 Reader 身份")
            require(
                not any("projected" in v for v in spec.get("volumes", [])),
                "控制台禁止挂载 Reader token",
            )
            require("CONNECTOR_READER_TOKENS" not in env, "控制台禁止加载 Reader 密钥")
    executor_accounts = [
        d
        for d in documents
        if d["kind"] == "ServiceAccount" and d["metadata"]["name"].endswith("-executor")
    ]
    require(len(executor_accounts) == 1, "必须保留独立且无授权的 Executor ServiceAccount")
    executor = executor_accounts[0]["metadata"]["name"]
    require(
        executor_accounts[0].get("automountServiceAccountToken") is False, "Executor 禁止自动 token"
    )
    for item in documents:
        if item["kind"] in {"Deployment", "Job"}:
            require(
                item["spec"]["template"]["spec"].get("serviceAccountName") != executor,
                "禁止运行 Executor Pod",
            )
    print(f"Helm 只读资源检查通过：{len(documents)} 个资源，Executor 无绑定/Pod/凭证", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--values", type=Path)
    args = parser.parse_args()
    helm = helm_path()
    if not (CHART / "charts/temporal-1.7.0.tgz").is_file():
        subprocess.run([helm, "dependency", "build", str(CHART)], cwd=ROOT, check=True)
    values = ["--values", str(args.values.resolve())] if args.values else []
    subprocess.run([helm, "lint", str(CHART), "--strict", *values], cwd=ROOT, check=True)
    rendered = subprocess.run(
        [helm, "template", "weipai", str(CHART), "--namespace", "weipai-ops", *values],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    parsed = subprocess.run(
        ["node", str(ROOT / "scripts/helm_documents.mjs")],
        input=rendered.stdout,
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    validate_documents(json.loads(parsed.stdout))
    if not args.values:
        # 真正渲染无效配置，确认错误会挡在部署之前。
        negative_cases = (
            ["--set", "executor.enabled=true"],
            ["--set-string", "images.backend=registry.invalid/backend:latest"],
            ["--set", "targetNamespaces={}"],
            ["--set", "targetNamespaces={observe,observe}"],
            ["--set", "discovery.intervalSeconds=0"],
            ["--set-string", "backendSecret=invalid/name"],
            [
                "--set-string",
                "temporal.server.config.persistence.datastores.default.sql.password=offline-only-placeholder",
            ],
            ["--set", "temporal.server.config.namespaces.useHelmHooks=true"],
        )
        for override in negative_cases:
            result = subprocess.run(
                [helm, "template", "weipai", str(CHART), *override],
                cwd=ROOT,
                capture_output=True,
            )
            require(result.returncode != 0, "Helm 未拒绝不安全或无效参数")
        print(f"Helm 无效配置拒绝检查通过：{len(negative_cases)} 项", flush=True)
    print("本地 Helm 检查通过；此命令未部署 ACK，也未验证真实 Discovery", flush=True)


if __name__ == "__main__":
    main()
