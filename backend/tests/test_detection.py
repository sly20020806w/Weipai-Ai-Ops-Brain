"""Step 23 离线检测验收，禁止真实 HTTP/DNS/socket。"""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError
from temporalio.client import Client, ScheduleActionStartWorkflow, ScheduleOverlapPolicy

from app.config import Settings
from app.connectors.kubernetes.fake import FakeKubernetesConnector, sample_snapshot
from app.connectors.observability.detection_fake import FakeDetectionPrometheusConnector
from app.connectors.observability.models import MetricPoint, MetricSeries, MetricsQuery
from app.tasks.states import TaskSource
from app.triggers.detection.config import DetectionConfig, StateRule, TrendRule
from app.triggers.detection.detectors import detect_state, detect_trend, detector_key
from app.triggers.detection.schedule import detection_schedule, ensure_detection_schedule
from app.triggers.detection.sources import DetectionSources
from tests.test_scheduling import FakeScheduleClient

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
END = datetime(2026, 10, 6, 2, tzinfo=UTC)


def trend(
    values: tuple[float, ...], *, kind: str = "capacity"
) -> tuple[TrendRule, MetricSeries, MetricsQuery]:
    rule = TrendRule.model_validate(
        {"rule_id": "disk", "metric_name": "disk_used_ratio", "kind": kind}
    )
    query = MetricsQuery(
        service_name="payment-service",
        metric_name="disk_used_ratio",
        start=END - timedelta(minutes=15),
        end=END,
    )
    series = MetricSeries(
        service_name=rule.service_name,
        metric_name=rule.metric_name,
        labels={"disk": "data"},
        points=tuple(
            MetricPoint(timestamp=END - timedelta(minutes=len(values) - i), value=value)
            for i, value in enumerate(values)
        ),
    )
    return rule, series, query


def test_replicas_current_baseline_desired_and_missing() -> None:
    deployment = sample_snapshot().deployments[0]
    value = detect_state(StateRule(), deployment, "ack-fake", END)
    assert value is not None and value.breached and value.source is TaskSource.STATE
    assert value.summary["current"] == 2
    assert value.summary["baseline"] == value.summary["desired"] == 3
    missing = detect_state(StateRule(), None, "ack-fake", END)
    assert missing is not None and missing.breached and missing.summary["missing"] is True
    desired = detect_state(StateRule(desired_replicas=5), deployment, "ack-fake", END)
    assert desired is not None and desired.summary["desired"] == 5
    tolerant = detect_state(StateRule(allowed_deficit=1), deployment, "ack-fake", END)
    assert tolerant is not None and not tolerant.breached
    baseline = detect_state(
        StateRule(baseline_replicas=4, desired_replicas=2), deployment, "ack-fake", END
    )
    assert baseline is not None and baseline.breached


def test_unobserved_generation_is_unknown_and_namespace_mismatch_rejected() -> None:
    deployment = sample_snapshot().deployments[0]
    pending = deployment.model_copy(
        update={"status": deployment.status.model_copy(update={"observed_generation": 6})}
    )
    assert detect_state(StateRule(), pending, "ack-fake", END) is None
    with pytest.raises(ValueError, match="命名空间"):
        detect_state(StateRule(namespace="wrong"), deployment, "ack-fake", END)


def test_linear_disk_exhaustion_time_equals_hand_calculation() -> None:
    rule, series, query = trend((0.5, 0.6, 0.7))
    result = detect_trend(rule, series, query)
    assert result is not None and result.breached and result.source is TaskSource.PREDICTION
    assert result.summary["slope_per_second"] == pytest.approx(0.1 / 60)
    assert result.summary["r_squared"] == pytest.approx(1.0)
    expected = END + timedelta(minutes=2)
    assert datetime.fromisoformat(str(result.summary["predicted_exhaustion_at"])) == expected
    assert expected.isoformat() in result.title


@pytest.mark.parametrize("values", [(0.4, 0.4, 0.4), (0.7, 0.6, 0.5), (0.1, 0.101, 0.102)])
def test_flat_decreasing_and_distant_capacity_do_not_trigger(values: tuple[float, ...]) -> None:
    result = detect_trend(*trend(values))
    assert result is not None and not result.breached
    assert result.summary["predicted_exhaustion_at"] is None


@pytest.mark.parametrize("kind", ["traffic", "cost"])
@pytest.mark.parametrize(
    "values,breached",
    [((100.0, 110.0, 120.0), True), ((180.0, 180.0, 180.0), True), ((100.0, 100.0, 100.0), False)],
)
def test_traffic_growth_and_cost_baseline(
    kind: str, values: tuple[float, ...], breached: bool
) -> None:
    result = detect_trend(*trend(values, kind=kind))
    assert result is not None and result.breached is breached


@pytest.mark.parametrize(
    "values,breached", [((1.0, 1.0, 1.0), True), ((0.5, 0.6, 0.7), True), ((0.3, 0.3, 0.3), False)]
)
def test_bottleneck_sustained_or_forecast(values: tuple[float, ...], breached: bool) -> None:
    result = detect_trend(*trend(values, kind="bottleneck"))
    assert result is not None and result.breached is breached


def test_window_staleness_sparse_noise_and_service_isolation() -> None:
    rule, series, query = trend((0.5, 0.6, 0.7))
    outside = MetricPoint(timestamp=END, value=999.0)
    extended = series.model_copy(update={"points": (*series.points, outside)})
    assert detect_trend(rule, extended, query) == detect_trend(rule, series, query)
    assert (
        detect_trend(rule, series.model_copy(update={"points": series.points[:2]}), query) is None
    )
    stale = query.model_copy(update={"end": END + timedelta(minutes=3)})
    assert detect_trend(rule, series, stale) is None
    assert detect_trend(*trend((0.2, 0.7, 0.3, 0.6, 0.2))) is None
    with pytest.raises(ValueError, match="规则不匹配"):
        detect_trend(rule, series.model_copy(update={"service_name": "other"}), query)
    with pytest.raises(ValueError, match="不能重复"):
        detect_trend(
            rule, series.model_copy(update={"points": (*series.points, series.points[-1])}), query
        )
    with pytest.raises(ValueError, match="非负"):
        detect_trend(*trend((-1.0, 0.0, 0.1)))


@pytest.mark.parametrize(
    "changes",
    [
        {"interval_seconds": 0},
        {"activity_max_attempts": 0},
        {"lookback_seconds": 120},
        {"schedule_id": "unsafe/id"},
        {"step_seconds": 0},
        {"unknown": True},
        {"state_rules": [{"baseline_replicas": -1}]},
        {
            "trend_rules": [
                {
                    "rule_id": "disk",
                    "metric_name": "disk_used_ratio",
                    "kind": "capacity",
                    "limit": float("nan"),
                }
            ]
        },
        {"state_rules": [{"rule_id": "disk"}]},
    ],
)
def test_invalid_configuration_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DetectionConfig.model_validate(changes)


def test_environment_configuration_and_threshold_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DETECTION_CONFIG",
        '{"interval_seconds":600,"trend_rules":[],"state_rules":[{"baseline_replicas":4}]}',
    )
    config = Settings(APP_ENV="test").detection_config
    assert config.interval_seconds == 600 and config.trend_rules == ()
    assert detector_key(StateRule(), "target") != detector_key(config.state_rules[0], "target")


@pytest.mark.asyncio
async def test_fake_sources_all_four_categories_and_normal_silence() -> None:
    snapshot = sample_snapshot()
    async with (
        FakeKubernetesConnector(snapshot) as kubernetes,
        FakeDetectionPrometheusConnector() as metrics,
    ):
        values = await DetectionSources(kubernetes, metrics).collect(DetectionConfig(), END)
        assert len(values) == 5 and all(value.breached for value in values)
        assert {value.summary["kind"] for value in values} == {
            "replicas",
            "capacity",
            "traffic",
            "cost",
            "bottleneck",
        }
    deployment = snapshot.deployments[0]
    ready = deployment.model_copy(
        update={"status": deployment.status.model_copy(update={"ready_replicas": 3})}
    )
    healthy = snapshot.model_copy(update={"deployments": (ready,)})
    async with (
        FakeKubernetesConnector(healthy) as kubernetes,
        FakeDetectionPrometheusConnector(healthy=True) as metrics,
    ):
        values = await DetectionSources(kubernetes, metrics).collect(DetectionConfig(), END)
        assert len(values) == 5 and not any(value.breached for value in values)


@pytest.mark.asyncio
async def test_schedule_registration_idempotent_and_disabled() -> None:
    config = DetectionConfig()
    schedule = detection_schedule(config, "queue")
    assert schedule.spec.intervals[0].every.total_seconds() == 300
    assert (
        schedule.policy.overlap is ScheduleOverlapPolicy.SKIP and schedule.policy.pause_on_failure
    )
    assert isinstance(schedule.action, ScheduleActionStartWorkflow)
    fake = FakeScheduleClient()
    client = cast(Client, fake)
    assert await ensure_detection_schedule(client, config, "queue")
    existing = fake.schedules[config.schedule_id]
    existing.state.paused = True
    assert not await ensure_detection_schedule(client, config, "different")
    assert fake.schedules[config.schedule_id] is existing and existing.state.paused
    assert not await ensure_detection_schedule(client, DetectionConfig(enabled=False), "queue")
    assert not await ensure_detection_schedule(
        client, DetectionConfig(state_rules=(), trend_rules=()), "queue"
    )
