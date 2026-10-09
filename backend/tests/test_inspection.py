"""全 Fake/HTTP mock，所有真实网络均被禁止。"""

from datetime import timedelta
from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.connectors.inspection.client import HTTPInspectionConnector, InspectionEndpoint
from app.connectors.inspection.factory import create_inspection_connector
from app.connectors.inspection.fake import FakeInspectionConnector, sample_facts
from app.connectors.inspection.models import InspectionFacts, InspectionQuery
from app.connectors.models import ReaderCredentials
from app.db.base import utc_now
from app.tasks.inspection.catalog import Mode
from app.tasks.inspection.engine import evaluate
from app.tasks.inspection.models import InspectionConfig
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import WorkflowInput

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def test_design_seventeen_areas_four_categories_and_exact_four_anomalies() -> None:
    snapshot = sample_facts("payment-service")
    results = evaluate(
        "payment-service", "inspection", InspectionConfig(), snapshot, uuid4(), utc_now()
    )
    assert len({check.area for check in results}) == 17
    assert {check.category for check in results} == {"stability", "capacity", "security", "cost"}
    assert {check.check_id for check in results if check.outcome == "abnormal"} == {
        "pdb_present",
        "hpa_present",
        "certificate_days",
        "ecs_idle",
    }
    assert all(check.evidence_id and check.source_reference for check in results)


@pytest.mark.parametrize("mode", ["inspection", "capacity", "governance"])
def test_healthy_environment_has_no_abnormal_or_unknown(mode: Mode) -> None:
    results = evaluate(
        "payment-service",
        mode,
        InspectionConfig(),
        sample_facts("payment-service", abnormal=False),
        uuid4(),
        utc_now(),
    )
    assert results and all(item.outcome == "healthy" for item in results)
    if mode == "capacity":
        assert {item.category for item in results} == {"capacity"}


@pytest.mark.parametrize(
    ("check_id", "bad_value"),
    [
        ("service_health", False),
        ("k8s_health", False),
        ("cloud_health", False),
        ("database_health", False),
        ("redis_health", False),
        ("mq_health", False),
        ("disk_usage", 0.85),
        ("network_health", False),
        ("monitoring_present", False),
        ("alerts_healthy", False),
        ("logs_healthy", False),
        ("traces_healthy", False),
        ("certificate_days", 7.0),
        ("dns_healthy", False),
        ("capacity_usage", 0.85),
        ("cost_daily_growth", 0.20),
        ("security_healthy", False),
        ("single_point", True),
        ("pdb_present", False),
        ("hpa_present", False),
        ("replicas", 1.0),
        ("runbook_present", False),
        ("capacity_growth", 0.20),
        ("capacity_exhaustion_days", 7.0),
        ("permissions_excessive", True),
        ("credential_risk", True),
        ("public_exposure", True),
        ("security_group_risk", True),
        ("ecs_idle", True),
        ("low_utilization", True),
        ("overprovisioned", True),
        ("temporary_unreclaimed", True),
    ],
)
def test_each_required_inspection_and_governance_signal_detects_anomaly(
    check_id: str,
    bad_value: bool | float,
) -> None:
    # 验收值独立于实现目录；删除检查、翻转布尔语义或放宽默认阈值都必须导致失败。
    snapshot = sample_facts("payment-service", abnormal=False)
    facts = tuple(
        f.model_copy(update={"value": bad_value}) if f.check_id == check_id else f
        for f in snapshot.facts
    )
    snapshot = snapshot.model_copy(update={"facts": facts})
    results = evaluate(
        "payment-service", "governance", InspectionConfig(), snapshot, uuid4(), utc_now()
    )
    assert [c.check_id for c in results if c.outcome == "abnormal"] == [check_id]


@pytest.mark.parametrize(
    "invalid", ["missing", "null", "stale", "future", "incomplete", "wrong-type"]
)
def test_unreliable_fact_is_unknown_not_healthy(invalid: str) -> None:
    snapshot = sample_facts("payment-service", abnormal=False)
    facts = list(snapshot.facts)
    if invalid == "missing":
        facts = facts[1:]
    elif invalid == "null":
        facts[0] = facts[0].model_copy(update={"value": None})
    elif invalid == "wrong-type":
        facts[0] = facts[0].model_copy(update={"value": 1.0})
    else:
        offset = timedelta(days=-1) if invalid == "stale" else timedelta(days=1)
        if invalid != "incomplete":
            facts[0] = facts[0].model_copy(update={"observed_at": utc_now() + offset})
    snapshot = snapshot.model_copy(
        update={"facts": tuple(facts), "complete": invalid != "incomplete"}
    )
    results = evaluate(
        "payment-service", "inspection", InspectionConfig(), snapshot, uuid4(), utc_now()
    )
    assert results[0].outcome == "unknown"


def test_wrong_service_unknown_check_duplicate_and_nonfinite_rejected() -> None:
    snapshot = sample_facts("payment-service")
    with pytest.raises(ValueError):
        evaluate("other-service", "inspection", InspectionConfig(), snapshot, uuid4(), utc_now())
    with pytest.raises(ValueError):
        InspectionFacts.model_validate(snapshot.model_copy(update={"facts": snapshot.facts * 2}))
    with pytest.raises(ValueError):
        evaluate(
            "payment-service",
            "inspection",
            InspectionConfig(),
            snapshot.model_copy(
                update={
                    "facts": (snapshot.facts[0].model_copy(update={"check_id": "unknown_check"}),)
                }
            ),
            uuid4(),
            utc_now(),
        )
    with pytest.raises(ValueError):
        type(snapshot.facts[0]).model_validate(
            snapshot.facts[0].model_copy(update={"value": float("nan")})
        )


def test_configuration_is_environment_only_and_threshold_changes_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv(
        "INSPECTION_CONFIG", '{"services":["payment-service"],"thresholds":{"certificate_days":5}}'
    )
    settings = Settings()
    results = evaluate(
        "payment-service",
        "inspection",
        settings.inspection_config,
        sample_facts("payment-service"),
        uuid4(),
        utc_now(),
    )
    assert next(c for c in results if c.check_id == "certificate_days").outcome == "healthy"
    invalid_configs: tuple[dict[str, object], ...] = (
        {"thresholds": {"pdb_present": 1.0}},
        {"services": []},
        {"max_age_seconds": 0},
    )
    for value in invalid_configs:
        with pytest.raises(ValidationError):
            Settings(APP_ENV="test", INSPECTION_CONFIG=value)


@pytest.mark.asyncio
async def test_fake_deep_copy_closed_and_reader_only() -> None:
    connector = FakeInspectionConnector()
    query = InspectionQuery(service_name="payment-service")
    first, second = await connector.query(query), await connector.query(query)
    assert first is not second and first.facts is not second.facts
    assert not hasattr(connector, "execute") and not hasattr(connector, "write")
    await connector.aclose()
    with pytest.raises(RuntimeError, match="关闭"):
        await connector.query(query)
    async with create_inspection_connector(Settings(APP_ENV="test")) as configured:
        assert isinstance(configured, FakeInspectionConnector)
    with pytest.raises(ValidationError):
        Settings(APP_ENV="test", CONNECTOR_MODE="real")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 302, 500])
async def test_http_read_only_protocol_scope_and_no_secret_leak(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://ops.example/readonly/facts?service_name=payment-service"
        assert request.headers["Authorization"] == "Bearer reader-secret"
        return httpx.Response(status, json=sample_facts("payment-service").model_dump(mode="json"))

    async with HTTPInspectionConnector(
        InspectionEndpoint(base_url="https://ops.example/readonly", path="facts"),
        ReaderCredentials(connector="inspection", token=SecretStr("reader-secret")),
        transport=httpx.MockTransport(respond),
    ) as connector:
        if status == 200:
            assert (await connector.query(InspectionQuery(service_name="payment-service"))).complete
        else:
            with pytest.raises(RuntimeError) as error:
                await connector.query(InspectionQuery(service_name="payment-service"))
            assert "reader-secret" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"inspection_mode": "unknown"},
        {"inspection_mode": "inspection", "execution_enabled": True},
        {"inspection_mode": "inspection", "release_id": "release-1"},
    ],
)
def test_inspection_input_cannot_enable_actions_or_mix_scenarios(
    changes: dict[str, object],
) -> None:
    from dataclasses import replace

    value = WorkflowInput(task_id=str(uuid4()))
    with pytest.raises(ValueError):
        validate_workflow_input(replace(value, **changes))  # type: ignore[arg-type]
