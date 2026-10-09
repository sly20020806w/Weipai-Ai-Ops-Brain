"""Step 55：全部注册路由、敏感入口和部署权限的负向安全验收。"""

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import httpx2 as httpx
import pytest
from fastapi.routing import APIRoute

from app.api.auth import DOCUMENT_PATHS
from app.api.main import create_app
from app.auth.identity import Principal
from app.auth.service import AuthService
from app.config import Settings, parse_database_url
from app.connectors.kubernetes.client import HTTPKubernetesConnector, KubernetesError
from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.models import ReaderCredentials
from app.db.session import Database
from app.triggers.schemas import EventOrigin
from tests.auth_support import auth_config

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
ROOT = Path(__file__).resolve().parents[2]
tools_spec = importlib.util.spec_from_file_location(
    "security_tools", ROOT / "scripts" / "security_tools.py"
)
assert tools_spec is not None and tools_spec.loader is not None
security_tools = importlib.util.module_from_spec(tools_spec)
sys.modules["security_tools"] = security_tools
tools_spec.loader.exec_module(security_tools)
spec = importlib.util.spec_from_file_location(
    "security_checks", ROOT / "scripts" / "security_checks.py"
)
assert spec is not None and spec.loader is not None
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)


@pytest.mark.parametrize(
    "message", ["could not read file", "cannot allocate memory", "permission denied"]
)
def test_secret_scan_read_errors_fail_even_with_zero_exit(message: str) -> None:
    result = subprocess.CompletedProcess(["gitleaks"], 0, "no leaks found", message)
    with pytest.raises(RuntimeError, match="范围不完整"):
        security.require_complete_secret_scan(result)


def registered_operations() -> list[tuple[str, str]]:
    app = create_app(Settings(APP_ENV="test"))

    def routes(items: list[Any]) -> list[APIRoute]:
        found = []
        for route in items:
            if isinstance(route, APIRoute):
                found.append(route)
            elif nested := getattr(route, "original_router", None):
                found.extend(routes(nested.routes))
        return found

    return [
        (method, route.path)
        for route in routes(app.routes)
        for method in sorted(route.methods or set())
        if route.path.startswith("/api/") and route.path != "/api/auth/login"
    ]


OPERATIONS = registered_operations()


@pytest.mark.parametrize("method,path", OPERATIONS)
@pytest.mark.asyncio
async def test_every_registered_api_rejects_anonymous_before_payload_validation(
    method: str,
    path: str,
) -> None:
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=auth_config()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.request(method, path, json={})
        assert response.status_code == 401, (method, path)
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", sorted(DOCUMENT_PATHS))
@pytest.mark.parametrize("suffix", ["", "/"])
@pytest.mark.asyncio
async def test_documentation_cannot_expose_contract_anonymously(path: str, suffix: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings(APP_ENV="test"))),
        base_url="http://127.0.0.1",
    ) as client:
        assert (await client.get(path + suffix)).status_code == 401
        assert (await client.get("/health")).status_code == 200


@pytest.mark.parametrize("origin", get_args(EventOrigin))
@pytest.mark.asyncio
async def test_all_webhook_origins_require_signature_even_with_cookie(origin: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings(APP_ENV="test"))),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/webhooks/" + origin,
            json={},
            headers={"Cookie": "ops_session=fake-cookie"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_documents_use_existing_session_and_writes_require_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = auth_config()
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=config))
    database = Database(parse_database_url("postgresql+asyncpg://127.0.0.1:1/unused"))
    app.state.database = database
    principal = Principal(
        "local-owner", uuid4(), datetime.now(UTC) + timedelta(minutes=5), "csrf-test"
    )

    async def authenticate(self: AuthService, cookie: str) -> Principal | None:
        return principal if cookie == "synthetic-session" else None

    monkeypatch.setattr(AuthService, "authenticate", authenticate)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Cookie": f"{config.cookie_name}=synthetic-session"},
        ) as client:
            for path in sorted(DOCUMENT_PATHS):
                response = await client.get(path)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
            for method, path in OPERATIONS:
                if method in {"POST", "PUT", "PATCH", "DELETE"}:
                    assert (await client.request(method, path, json={})).status_code == 403
    finally:
        await database.dispose()


def policy(filename: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        (ROOT / "deploy" / "security" / filename).read_text(encoding="utf-8")
    )
    return result


def matches(patterns: str | list[str], value: str) -> bool:
    return any(
        fnmatchcase(value, item) for item in ([patterns] if isinstance(patterns, str) else patterns)
    )


def ram_allowed(documents: list[dict[str, Any]], action: str, resource: str) -> bool:
    """仅评估这些无 Condition 的身份策略；真实 RAM 授权在 ACK 上线阶段验收。"""
    allowed = False
    for document in documents:
        assert document["Version"] == "1"
        for statement in document["Statement"]:
            assert set(statement) <= {"Effect", "Action", "NotAction", "Resource"}
            action_match = (
                matches(statement["Action"], action)
                if "Action" in statement
                else not matches(statement["NotAction"], action)
            )
            if action_match and matches(statement["Resource"], resource):
                if statement["Effect"] == "Deny":
                    return False
                allowed = True
    return allowed


@pytest.mark.parametrize(
    "action",
    [
        "ecs:StartInstance",
        "ecs:RunInstances",
        "ecs:DeleteInstance",
        "rds:RestartDBInstance",
        "rds:DeleteDBInstance",
        "kvstore:FlushInstance",
        "mq:PublishMessage",
        "slb:SetLoadBalancerStatus",
        "vpc:DeleteVpc",
        "alidns:UpdateDomainRecord",
        "cdn:DeleteCdnDomain",
        "log:PutLogs",
        "ram:PassRole",
        "ram:CreateAccessKey",
        "sts:AssumeRole",
        "kms:Decrypt",
        "future:WriteAction",
    ],
)
def test_ram_reader_explicitly_denies_writes_and_escalation_even_if_other_policy_allows(
    action: str,
) -> None:
    extra = {"Version": "1", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
    assert not ram_allowed([policy("ram-reader.json"), extra], action, "acs:test:any")
    assert not ram_allowed([policy("ram-executor.json"), extra], action, "acs:test:any")


def test_ram_exact_read_actions_and_resource_scope() -> None:
    reader = policy("ram-reader.json")
    allow = {
        action
        for statement in reader["Statement"]
        if statement["Effect"] == "Allow"
        for action in statement["Action"]
    }
    assert allow == set(reader["Statement"][0]["NotAction"])
    assert len(allow) == 14 and all("*" not in action for action in allow)
    # MQ 的 RAM Action 与 OpenAPI 名称不同，不能机械拼接 API 名称。
    assert {"mq:QueryInstanceBaseInfo", "mq:ListTopic"} <= allow
    resources = {
        key: "acs:synthetic:" + key
        for key in [
            "ECS_RESOURCE",
            "RDS_RESOURCE",
            "REDIS_RESOURCE",
            "MQ_RESOURCE",
            "SLB_RESOURCE",
            "VPC_RESOURCE",
            "CDN_RESOURCE",
            "LOG_RESOURCE",
        ]
    }
    rendered = security.render_policy("ram-reader.json", resources)
    assert ram_allowed([rendered], "rds:DescribeDBInstancePerformance", resources["RDS_RESOURCE"])
    assert not ram_allowed([rendered], "rds:DescribeDBInstancePerformance", "acs:synthetic:other")
    assert not ram_allowed([policy("ram-executor.json")], "rds:DescribeDBInstancePerformance", "*")


def test_kubernetes_reader_has_no_write_secret_or_escalation_grants() -> None:
    document = security.render_policy(
        "kubernetes.json",
        {
            "PLATFORM_NAMESPACE": "test-platform",
            "TARGET_NAMESPACE": "test-target",
            "BINDING_PREFIX": "test",
        },
    )
    accounts = [item for item in document["items"] if item["kind"] == "ServiceAccount"]
    assert {item["metadata"]["name"] for item in accounts} == {"ai-reader", "ai-executor"}
    assert all(item["automountServiceAccountToken"] is False for item in accounts)
    assert "${" not in json.dumps(document)
    assert all(
        item["metadata"]["name"].startswith("test-")
        for item in document["items"]
        if item["kind"] != "ServiceAccount"
    )
    for item in document["items"]:
        for rule in item.get("rules", []):
            assert set(rule["verbs"]) <= {"get", "list", "watch"}
            assert set(rule["resources"]) <= {"pods", "events", "deployments", "namespaces"}
            assert "*" not in rule["apiGroups"]
        for subject in item.get("subjects", []):
            assert subject["name"] == "ai-reader"
    assert {
        item["metadata"]["namespace"] for item in document["items"] if item["kind"] == "RoleBinding"
    } == {"test-target"}


def fake_locks(root: Path) -> dict[str, Any]:
    (root / "backend").mkdir()
    (root / "frontend").mkdir()
    (root / "backend" / "uv.lock").write_text(
        '[[package]]\nname = "synthetic"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    (root / "frontend" / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n\npackages:\n\n"
        "  '@scope/synthetic@1.0.0':\n    resolution: {}\n\nsnapshots:\n",
        encoding="utf-8",
    )
    return {
        "results": [
            {
                "source": {"path": str(root / "backend" / "uv.lock")},
                "packages": [
                    {"package": {"name": "synthetic", "version": "1.0", "ecosystem": "PyPI"}}
                ],
            },
            {
                "source": {"path": str(root / "frontend" / "pnpm-lock.yaml")},
                "packages": [
                    {
                        "package": {
                            "name": "@scope/synthetic",
                            "version": "1.0.0",
                            "ecosystem": "npm",
                        }
                    }
                ],
            },
        ]
    }


@pytest.mark.parametrize(
    "failure", ["empty", "missing", "wrong_version", "vulnerability", "unknown_severity"]
)
def test_dependency_gate_rejects_empty_partial_wrong_and_vulnerable_reports(
    tmp_path: Path, failure: str
) -> None:
    report = fake_locks(tmp_path)
    assert security.verify_osv_report(tmp_path, report)["vulnerabilities"] == 0
    match failure:
        case "empty":
            report["results"] = []
        case "missing":
            report["results"].pop()
        case "wrong_version":
            report["results"][0]["packages"][0]["package"]["version"] = "0.9"
        case _:
            report["results"][0]["packages"][0]["vulnerabilities"] = [{"id": "synthetic-advisory"}]
    with pytest.raises(ValueError):
        security.verify_osv_report(tmp_path, report)


def test_missing_policy_values_fail_closed() -> None:
    with pytest.raises(KeyError):
        security.render_policy("kubernetes.json", {})


@pytest.mark.parametrize("failure", ["remote_docker", "not_kind", "remote_kubeconfig"])
def test_rbac_lab_rejects_nonlocal_targets_before_mutations(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], *, data: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["docker", "context", "inspect"]:
            value: object = [
                {
                    "Endpoints": {
                        "docker": {
                            "Host": "ssh://remote"
                            if failure == "remote_docker"
                            else "npipe://local"
                        }
                    }
                }
            ]
        elif command[:2] == ["docker", "inspect"]:
            value = [
                {
                    "Config": {
                        "Labels": {
                            "io.x-k8s.kind.role": "worker"
                            if failure == "not_kind"
                            else "control-plane"
                        }
                    }
                }
            ]
        else:
            assert "create" not in command and "delete" not in command
            value = {
                "clusters": [{"cluster": {"server": "https://production.example.invalid:6443"}}]
            }
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(security, "run", fake_run)
    with pytest.raises(ValueError):
        security.check_local_rbac("local-kind-control-plane")
    assert all("create" not in command and "delete" not in command for command in calls)


@pytest.mark.parametrize("names", [[], ["prod", "prod"], ["*"], ["prod/other"], ["Prod"]])
def test_invalid_reader_namespace_scope_rejected(names: list[str]) -> None:
    with pytest.raises(ValueError):
        KubernetesConfig(
            cluster_name="local", base_url="https://k8s.example.invalid", namespace_allowlist=names
        )


@pytest.mark.asyncio
async def test_namespace_scoped_discovery_and_queries_never_fetch_outside_scope() -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.url.path == "/api/v1/namespaces"
        return httpx.Response(
            200,
            json={
                "apiVersion": "v1",
                "kind": "NamespaceList",
                "metadata": {"resourceVersion": "1"},
                "items": [
                    {
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "metadata": {"name": name, "uid": name},
                    }
                    for name in ("prod", "kube-system")
                ],
            },
        )

    connector = HTTPKubernetesConnector(
        KubernetesConfig(
            cluster_name="local",
            base_url="https://k8s.example.invalid",
            namespace_allowlist=("prod",),
        ),
        ReaderCredentials(connector="kubernetes", token="synthetic-reader"),
        transport=httpx.MockTransport(handle),
    )
    try:
        assert await connector.list_namespaces() == ("prod",)
        for query in (
            connector.list_deployments,
            connector.list_pods,
            connector.list_events,
            connector.watch_events,
        ):
            with pytest.raises(KubernetesError, match="白名单"):
                await query("kube-system")
        assert requests == ["/api/v1/namespaces"]
    finally:
        await connector.aclose()


def test_route_audit_covers_openapi_operation_set() -> None:
    # 动态枚举所有注册方法，未来新增 API 自动纳入负向验收。
    schema = create_app(Settings(APP_ENV="test")).openapi()
    expected = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        if path.startswith("/api/") and path != "/api/auth/login"
        for method in operations
    }
    assert set(OPERATIONS) == expected
    assert len(OPERATIONS) >= 58
