"""Step 57：只读云端配置在离线环境验收，全部外部请求禁止真实联网。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.api.main import create_app
from app.auth.identity import Principal
from app.auth.service import AuthService
from app.config import Settings
from app.connectors.kubernetes.factory import create_kubernetes_connector
from app.connectors.models import ReaderCredentials
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyAction, PolicyConfig, PolicyDecision, PolicyRule, RiskLevel
from app.tasks.readonly import create_readonly_worker, validate_readonly_settings
from tests.auth_support import auth_config
from tests.test_events import SECRET, alert_body, signed

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def cloud_settings(**updates: Any) -> Settings:
    return Settings.model_validate(
        {
            "APP_ENV": "production",
            "OPS_READ_ONLY": True,
            "WORKER_MODE": "readonly_discovery",
            "CONNECTOR_MODE": "real",
            "DATABASE_URL": "postgresql+asyncpg://127.0.0.1:5432/unused",
            "CONNECTOR_READER_TOKENS": dict.fromkeys(
                ("ops_platform", "kubernetes", "arms", "git", "cloud"), "offline-reader-token"
            ),
            "OPS_PLATFORM_CONFIG": {
                "base_url": "https://ops.example.invalid",
                "service_tree_path": "tree",
                "applications_path": "applications",
                "owners_path": "owners",
                "tickets_path": "tickets",
            },
            "KUBERNETES_CONFIG": {
                "cluster_name": "ack-test",
                "base_url": "https://kubernetes.default.svc",
                "namespace_allowlist": ["observe"],
            },
            "ARMS_CONFIG": {"base_url": "https://arms.example.invalid", "region_id": "cn-hangzhou"},
            "GIT_CONFIG": {
                "base_url": "https://git.example.invalid",
                "provider": "gitlab",
                "services": {"payment-service": "group/payment"},
            },
            "CLOUD_CONFIG": {
                "endpoints": {"cms": "https://cms.example.invalid"},
                "services": {"payment-service": []},
            },
            **updates,
        }
    )


@pytest.mark.parametrize("environment", ["local", "test", "staging", "production"])
@pytest.mark.parametrize("risk", list(RiskLevel))
def test_readonly_policy_cannot_be_relaxed_by_allow_rule(environment: str, risk: RiskLevel) -> None:
    settings = Settings(
        APP_ENV=environment,
        OPS_READ_ONLY=True,
        POLICY_CONFIG=PolicyConfig(
            rules=(
                PolicyRule(
                    id="user_allow",
                    risk_levels=tuple(RiskLevel),
                    decision=PolicyDecision.ALLOW,
                    reason="测试覆盖",
                ),
                PolicyRule(
                    id="readonly_guard",
                    risk_levels=tuple(RiskLevel),
                    decision=PolicyDecision.ALLOW,
                    reason="同名覆盖尝试",
                ),
            )
        ),
    )
    result = create_policy_engine(settings).evaluate(
        PolicyAction(name="arbitrary_action", risk_level=risk)
    )
    assert result.decision is (
        PolicyDecision.ALLOW if risk is RiskLevel.L0 else PolicyDecision.DENY
    )
    assert (
        create_policy_engine(settings).evaluate(PolicyAction(name="undeclared")).decision
        is PolicyDecision.DENY
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"EXECUTION_CONFIG": {"enabled": True}},
        {
            "EXECUTION_CONFIG": {
                "bindings": [
                    {
                        "service_name": "payment-service",
                        "cluster_name": "ack",
                        "namespace": "observe",
                        "deployment_name": "payment",
                        "container_name": "app",
                        "images": {"v1": "image:v1"},
                    }
                ]
            }
        },
        {"AGENT_CONFIG": {"enabled": True}},
        {"OPS_READ_ONLY": False},
    ],
)
def test_unsafe_startup_configuration_rejected(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        cloud_settings(**updates)


@pytest.mark.parametrize(
    "field",
    [
        "OPS_PLATFORM_CONFIG",
        "KUBERNETES_CONFIG",
        "ARMS_CONFIG",
        "GIT_CONFIG",
        "CLOUD_CONFIG",
        "DATABASE_URL",
    ],
)
def test_missing_production_config_fails_before_network(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        validate_readonly_settings(cloud_settings(**{field: None}))


def test_worker_requires_real_sources_scope_and_exact_reader_identities() -> None:
    validate_readonly_settings(cloud_settings())
    for changes in (
        {"CONNECTOR_MODE": "fake"},
        {
            "KUBERNETES_CONFIG": {
                "cluster_name": "ack",
                "base_url": "https://kubernetes.default.svc",
            }
        },
        {"CONNECTOR_READER_TOKENS": {"executor": "offline-token"}},
    ):
        with pytest.raises(ValueError):
            validate_readonly_settings(cloud_settings(**changes))
    with pytest.raises(ValueError):
        validate_readonly_settings(Settings(APP_ENV="test", OPS_READ_ONLY=True))
    with pytest.raises(ValueError):
        validate_readonly_settings(
            cloud_settings().model_copy(
                update={"execution_config": Mock(enabled=True, bindings=())}
            )
        )


def test_production_registry_has_no_executor_or_task_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor = Mock()
    monkeypatch.setattr("app.tasks.readonly.Worker", constructor)
    create_readonly_worker(Mock(), Mock(), cloud_settings())
    options = constructor.call_args.kwargs
    assert [workflow.__name__ for workflow in options["workflows"]] == ["DiscoveryWorkflow"]
    assert len(options["activities"]) == 1
    assert options["activities"][0].__name__ == "refresh"


def test_projected_token_reloaded_for_each_connector_and_errors_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file, ca_file = tmp_path / "token", tmp_path / "ca.crt"
    ca_file.write_text("offline-ca", encoding="utf-8")
    tokens = dict.fromkeys(("ops_platform", "arms", "git", "cloud"), "offline-reader-token")
    settings = cloud_settings(
        CONNECTOR_READER_TOKENS=tokens,
        KUBERNETES_READER_TOKEN_FILE=str(token_file),
        KUBERNETES_READER_CA_FILE=str(ca_file),
    )
    validate_readonly_settings(settings)
    constructor = Mock()
    monkeypatch.setattr("app.connectors.kubernetes.factory.HTTPKubernetesConnector", constructor)
    for token in ("offline-first", "offline-rotated"):
        token_file.write_text(token, encoding="utf-8")
        create_kubernetes_connector(settings)
        credentials: ReaderCredentials = constructor.call_args.args[1]
        assert credentials.token.get_secret_value() == token
        assert constructor.call_args.args[0].ca_cert_pem == "offline-ca"
    token_file.unlink()
    with pytest.raises(ValueError, match="无法读取") as error:
        create_kubernetes_connector(settings)
    assert str(token_file) not in str(error.value)
    with pytest.raises(ValueError, match="同时配置"):
        create_kubernetes_connector(
            settings.model_copy(
                update={
                    "connector_reader_tokens": {
                        **settings.connector_reader_tokens,
                        "kubernetes": credentials.token,
                    }
                }
            )
        )


@pytest.mark.asyncio
async def test_readonly_http_keeps_auth_and_reads_but_blocks_mutations_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal(
        "local-owner", uuid4(), datetime.now(UTC) + timedelta(hours=1), "offline-csrf"
    )

    async def authenticate(self: AuthService, cookie: str) -> Principal:
        return principal

    monkeypatch.setattr(AuthService, "authenticate", authenticate)
    app = create_app(
        Settings(
            APP_ENV="test",
            OPS_READ_ONLY=True,
            AUTH_CONFIG=auth_config(),
            TRIGGER_CONFIG={"webhook_secrets": {"prometheus": SECRET}},
        )
    )
    app.state.database = Mock()

    # 无数据库和 Temporal 接入能力，任何误调用都会使测试失败。
    @app.get("/api/readonly-probe")
    async def read_probe() -> dict[str, str]:
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.post("/api/chat", json={})).status_code == 401
        assert (await client.post("/webhooks/prometheus", json={})).status_code == 401
        body = alert_body()
        assert (
            await client.post("/webhooks/prometheus", content=body, headers=signed(body))
        ).status_code == 403
        assert (
            await client.post("/api/auth/login", json={}, headers={"X-Ops-Login": "1"})
        ).status_code == 422
        assert (await client.get("/health")).status_code == 200
        client.cookies.set("ops_session", "offline-cookie")
        assert (await client.get("/api/readonly-probe")).json() == {"status": "ok"}
        for method, path in (
            ("POST", "/api/chat"),
            ("POST", "/api/knowledge"),
            ("PUT", "/api/knowledge/invalid"),
            ("DELETE", "/api/runbooks/invalid"),
            ("POST", "/api/tasks/invalid/approval"),
        ):
            response = await client.request(
                method, path, headers={"X-CSRF-Token": principal.csrf_token}, json={}
            )
            assert response.status_code == 403
            assert "首次只读上线" in response.json()["detail"]
        assert (await client.post("/api/chat", json={})).status_code == 403
