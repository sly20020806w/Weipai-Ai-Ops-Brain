"""保障规则与严格契约：只用 Fake，禁止真实外部连接。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.connectors.war_room.facts import FakeWarRoomConnector, WarRoomFacts, WarRoomQuery
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyAction, PolicyDecision, RiskLevel
from app.tasks.inspection.models import InspectionConfig
from app.tasks.war_room.engine import assess_facts
from app.tasks.war_room.models import WarRoomConfig, WarRoomSubmission
from app.tasks.war_room.scenario import fake_resources
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import WorkflowInput

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("forbid_llm_network")]


def value() -> WarRoomSubmission:
    now = datetime.now(UTC)
    return WarRoomSubmission(
        request_id=uuid4(),
        service_name="payment-service",
        title="支付活动",
        kind="event",
        start=now - timedelta(seconds=1),
        end=now + timedelta(seconds=10),
        projected_rps=1000.0,
    )


async def facts(submission: WarRoomSubmission) -> WarRoomFacts:
    return await FakeWarRoomConnector().query(
        WarRoomQuery(
            service_name=submission.service_name,
            purpose="prepare",
            start=submission.start,
            end=submission.end,
        )
    )


async def test_capacity_projection_and_risk_scan_cover_all_areas() -> None:
    submission = value()
    resources, _ = fake_resources()
    target = resources.targets[submission.service_name]
    checks, required, known, safe, complete, _, _ = assess_facts(
        submission,
        WarRoomConfig(),
        InspectionConfig(),
        await facts(submission),
        target,
        target,
        uuid4(),
        datetime.now(UTC),
        purpose="prepare",
        owned=None,
    )
    assert required == 5 and known and safe and complete
    assert len(checks) == 32 and {c.category for c in checks} == {
        "stability",
        "capacity",
        "security",
        "cost",
    }
    assert len({c.area for c in checks}) == 17
    assert all(c.evidence_id and c.source_reference for c in checks)


@pytest.mark.parametrize(
    "change", ["missing_capacity", "partial", "stale", "future", "unknown_ready", "no_rollback"]
)
async def test_unknown_or_unsafe_facts_never_allow_preparation(change: str) -> None:
    submission = value()
    resource, _ = fake_resources()
    target = resource.targets[submission.service_name]
    snapshot = await facts(submission)
    if change == "missing_capacity":
        snapshot = snapshot.model_copy(update={"per_replica_rps": None})
    elif change == "partial":
        snapshot = snapshot.model_copy(
            update={"inspection": snapshot.inspection.model_copy(update={"complete": False})}
        )
    elif change in {"stale", "future"}:
        snapshot = snapshot.model_copy(
            update={
                "observed_at": datetime.now(UTC)
                + timedelta(seconds=10 if change == "future" else -1000)
            }
        )
    elif change == "unknown_ready":
        snapshot = snapshot.model_copy(update={"ready_replicas": None})
    else:
        snapshot = snapshot.model_copy(update={"rollback_ready": False})
    evaluated = assess_facts(
        submission,
        WarRoomConfig(),
        InspectionConfig(),
        snapshot,
        target,
        target,
        uuid4(),
        datetime.now(UTC),
        purpose="prepare",
        owned=None,
    )
    assert not evaluated[3]


@pytest.mark.parametrize("condition", ["not_ended", "ownership", "high_load", "unhealthy"])
async def test_cleanup_requires_end_owned_resources_and_remaining_capacity(condition: str) -> None:
    submission = value()
    resource, _ = fake_resources()
    baseline = resource.targets[submission.service_name]
    owned = baseline.model_copy(update={"replicas": 5, "resource_version": "8"})
    current = owned
    now = submission.end + timedelta(seconds=1)
    snapshot = (await facts(submission)).model_copy(
        update={
            "observed_at": now,
            "inspection": (await facts(submission)).inspection.model_copy(
                update={
                    "facts": tuple(
                        f.model_copy(update={"observed_at": now})
                        for f in (await facts(submission)).inspection.facts
                    )
                }
            ),
        }
    )
    if condition == "not_ended":
        now = submission.end - timedelta(seconds=1)
    elif condition == "ownership":
        current = owned.model_copy(update={"resource_version": "99"})
    elif condition == "high_load":
        snapshot = snapshot.model_copy(update={"current_rps": 2000.0})
    else:
        snapshot = snapshot.model_copy(update={"rollback_ready": False})
    evaluated = assess_facts(
        submission,
        WarRoomConfig(),
        InspectionConfig(),
        snapshot,
        current,
        baseline,
        uuid4(),
        now,
        purpose="cleanup",
        owned=owned,
    )
    assert not evaluated[3]


@pytest.mark.parametrize(
    "field, invalid",
    [
        ("projected_rps", float("nan")),
        ("title", "  "),
        ("start", datetime(2026, 1, 1)),
        ("kind", "other"),
    ],
)
async def test_submission_rejects_invalid_fields(field: str, invalid: object) -> None:
    data = value().model_dump()
    data[field] = invalid
    with pytest.raises(ValidationError):
        WarRoomSubmission.model_validate(data)


async def test_timezone_normalizes_and_zero_duration_rejected() -> None:
    original = value()
    assert original.start.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError):
        WarRoomSubmission.model_validate(original.model_copy(update={"end": original.start}))


async def test_default_policy_keeps_both_scale_actions_at_l3_approval() -> None:
    decision = create_policy_engine(Settings(APP_ENV="test")).evaluate(
        PolicyAction(name="scale_service", risk_level=RiskLevel.L3)
    )
    assert decision.decision is PolicyDecision.NEED_APPROVAL


@pytest.mark.parametrize(
    "mixed",
    [
        {"ticket_id": "T1"},
        {"architecture_review": True},
        {"inspection_mode": "inspection"},
        {"war_room": "yes"},
    ],
)
async def test_workflow_does_not_mix_scenarios(mixed: dict[str, object]) -> None:
    data: dict[str, object] = {"task_id": str(uuid4()), "war_room": True} | mixed
    with pytest.raises(ValueError):
        validate_workflow_input(WorkflowInput(**data))  # type: ignore[arg-type]


async def test_fake_window_injection_stays_in_requested_window() -> None:
    submission = value()
    connector = FakeWarRoomConnector(abnormal_windows=frozenset({1}))
    query = WarRoomQuery(
        service_name=submission.service_name,
        purpose="watch",
        window=0,
        start=submission.start,
        end=submission.end,
    )
    first = await connector.query(query)
    second = await connector.query(query.model_copy(update={"window": 1}))
    assert all(f.value is True for f in first.inspection.facts if f.check_id == "service_health")
    assert any(f.value is False for f in second.inspection.facts if f.check_id == "service_health")
