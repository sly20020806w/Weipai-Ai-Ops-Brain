"""离线分组与 Schedule 验收；不能访问源系统或真实网络。"""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from temporalio.client import Client, ScheduleActionStartWorkflow, ScheduleOverlapPolicy

from app.config import Settings
from app.learning.automation.models import (
    AutomationConfig,
    ManualOperation,
    Repetition,
    ScanWindow,
    Suggestion,
    WorkKind,
    WorkRecord,
    group_records,
)
from app.learning.automation.schedule import automation_schedule, ensure_automation_schedule
from app.learning.automation.sources import event_record, manual_record
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.tasks.states import TaskSource
from app.triggers.models import OpsEvent
from tests.test_scheduling import FakeScheduleClient

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def record(kind: WorkKind = WorkKind.MANUAL, service: str = "payment-service") -> WorkRecord:
    return WorkRecord(
        kind=kind,
        service_name=service,
        signature="检查连接池",
        table="audit_log",
        record_id=uuid4(),
        task_id=uuid4(),
        occurred_at=NOW,
        evidence_id=uuid4(),
    )


@pytest.mark.parametrize("kind", list(WorkKind))
def test_five_records_one_group_four_none_and_duplicates_not_counted(kind: WorkKind) -> None:
    records = tuple(record(kind) for _ in range(5))
    assert group_records(records[:4], 5) == ()
    groups = group_records((*records, records[0]), 5)
    assert len(groups) == 1 and len(groups[0].records) == 5
    assert {r.record_id for r in groups[0].records} == {r.record_id for r in records}


def test_services_types_content_and_order_are_isolated() -> None:
    records = tuple(record() for _ in range(5))
    normalized = records[0].model_copy(update={"signature": "  检查连接池  "})
    assert normalized.group_key == records[0].group_key
    assert group_records(records, 5) == group_records(tuple(reversed(records)), 5)
    assert group_records((*records[:4], record(service="other-service")), 5) == ()
    assert group_records((*records[:4], record(WorkKind.TICKET)), 5) == ()
    assert (
        group_records((*records[:4], records[4].model_copy(update={"signature": "检查磁盘"})), 5)
        == ()
    )


def test_conflicting_records_and_mixed_group_rejected() -> None:
    first = record()
    with pytest.raises(ValueError, match="冲突"):
        group_records((first, first.model_copy(update={"signature": "other"})), 5)
    with pytest.raises(ValueError):
        Repetition(group_key=first.group_key, records=(first, record(service="other")))


@pytest.mark.parametrize(
    "config",
    [
        {"threshold": 0},
        {"threshold": True},
        {"threshold": 1},
        {"interval_seconds": 0},
        {"lookback_seconds": 0},
        {"schedule_id": "unsafe/id"},
        {"activity_max_attempts": 0},
        {"activity_timeout_seconds": 0},
        {"unknown": True},
    ],
)
def test_invalid_config(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AutomationConfig.model_validate(config)


def test_window_timezone_and_invalid_ranges() -> None:
    window = ScanWindow.model_validate_json(
        '{"start":"2026-10-07T08:00:00+08:00","end":"2026-10-07T09:00:00+08:00"}'
    )
    assert window.start == NOW and window.end.tzinfo is UTC
    for start, end in (
        (NOW, NOW),
        (NOW, NOW - timedelta(seconds=1)),
        (NOW.replace(tzinfo=None), NOW),
    ):
        with pytest.raises(ValidationError):
            ScanWindow(start=start, end=end)


def test_environment_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOMATION_CONFIG", '{"threshold":7,"interval_seconds":600}')
    assert Settings(APP_ENV="test").automation_config.threshold == 7
    assert Settings(APP_ENV="test").automation_config.interval_seconds == 600


@pytest.mark.asyncio
async def test_schedule_uses_temporal_and_preserves_paused_existing() -> None:
    config = AutomationConfig()
    schedule = automation_schedule(config, "queue")
    assert schedule.spec.intervals[0].every.total_seconds() == 3600
    assert schedule.spec.time_zone_name == "UTC"
    assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
    assert schedule.policy.pause_on_failure
    assert isinstance(schedule.action, ScheduleActionStartWorkflow)
    fake = FakeScheduleClient()
    client = cast(Client, fake)
    assert await ensure_automation_schedule(client, config, "queue")
    existing = fake.schedules[config.schedule_id]
    existing.state.paused = True
    assert not await ensure_automation_schedule(client, config, "other-queue")
    assert existing is fake.schedules[config.schedule_id] and existing.state.paused
    assert not await ensure_automation_schedule(client, AutomationConfig(enabled=False), "queue")


@pytest.mark.parametrize("source", [TaskSource.TICKET, TaskSource.SCHEDULE, TaskSource.RELEASE])
def test_event_classification_does_not_count_every_release_as_a_check(source: TaskSource) -> None:
    event = OpsEvent(
        id=uuid4(),
        task_id=uuid4(),
        source=source.value,
        origin="schedule",
        external_id="release-verification:abc",
        service_name="payment-service",
        title="权限申请",
        occurred_at=NOW,
    )
    result = event_record(event)
    if source is TaskSource.RELEASE:
        assert result is None
        return
    assert result is not None
    assert result.kind is (
        WorkKind.TICKET if source is TaskSource.TICKET else WorkKind.RELEASE_CHECK
    )
    event.origin = "learning"
    assert event_record(event) is None
    event.origin = "argocd"
    if source is not TaskSource.TICKET:
        assert event_record(event) is None


def test_suggestion_is_only_an_evaluation_and_cannot_grant_execution() -> None:
    first = record()
    suggestion = Suggestion(
        repetition=Repetition(group_key=first.group_key, records=(first,)),
        method="self_healing",
        conclusion="评估自愈",
    )
    assert (
        "Policy" in suggestion.execution_requirement
        and "独立 Verifier" in suggestion.execution_requirement
    )
    with pytest.raises(ValidationError):
        Suggestion.model_validate({**suggestion.model_dump(), "approved": True})


@pytest.mark.parametrize("replay", [False, True])
def test_manual_source_requires_matching_success_audit_and_excludes_replay(replay: bool) -> None:
    value = ManualOperation(
        task_id=uuid4(),
        record_key="manual-1",
        service_name="payment-service",
        operation="check-pool",
        actor="operator",
        source_reference="fake://manual/1",
        occurred_at=NOW,
    )
    evidence = Evidence(
        id=uuid4(),
        task_id=value.task_id,
        source_tool="manual.operation",
        result_snapshot=value.model_dump(mode="json"),
    )
    audit = AuditRecord(
        id=uuid4(),
        task_id=value.task_id,
        evidence_id=evidence.id,
        event_type=AuditEventType.HUMAN_INTERACTION,
        actor="operator",
        operation="manual.operation",
        outcome="recorded",
        occurred_at=NOW,
        details={"mode": "replay"} if replay else {},
    )
    result = manual_record(audit, evidence, None)
    assert (result is None) == replay
    if result:
        assert result.evidence_id == evidence.id and result.record_id == audit.id
    audit.outcome = "failed"
    assert manual_record(audit, evidence, None) is None
