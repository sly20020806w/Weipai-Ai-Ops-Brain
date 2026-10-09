"""十项手算指标、未知标签、历史可用时间和无 live 实现的注册表。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db.base import utc_now
from app.learning.evaluation.metrics import EvaluationWindow, MetricSample, calculate
from app.learning.evaluation.models import EvaluationLabel, root_cause_hit
from app.learning.evaluation.replay import available, successful
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.tools.replay import replay_registry

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def sample_metrics() -> tuple[MetricSample, ...]:
    return (
        MetricSample(
            task_id=uuid4(),
            rca_correct=True,
            runbook_attempted=True,
            runbook_hit=True,
            execution_attempted=True,
            execution_verified=True,
            approval_decisions=2,
            alert=True,
            false_alert=False,
            mttr_seconds=60.0,
            tool_calls=4,
            verification_attempts=1,
            automated=True,
        ),
        MetricSample(
            task_id=uuid4(),
            rca_correct=False,
            runbook_attempted=True,
            execution_attempted=True,
            approval_decisions=2,
            approval_rejections=1,
            human_takeover=True,
            alert=True,
            false_alert=True,
            tool_calls=6,
            verification_attempts=2,
            verification_failures=1,
        ),
        MetricSample(
            task_id=uuid4(),
            rca_correct=True,
            runbook_attempted=True,
            runbook_hit=True,
            mttr_seconds=120.0,
            automated=True,
        ),
        MetricSample(task_id=uuid4(), alert=True, tool_calls=2),
    )


@pytest.mark.parametrize(
    "index,expected,numerator,denominator",
    [
        (0, 2 / 3, 2, 3),
        (1, 2 / 3, 2, 3),
        (2, 1 / 2, 1, 2),
        (3, 1 / 4, 1, 4),
        (4, 1 / 4, 1, 4),
        (5, 1 / 2, 1, 2),
        (6, 90, 180, 2),
        (7, 3, 12, 4),
        (8, 1 / 3, 1, 3),
        (9, 1 / 2, 2, 4),
    ],
)
def test_ten_metrics_match_independent_hand_calculation(
    index: int, expected: float, numerator: int, denominator: int
) -> None:
    metric = calculate(sample_metrics()).metrics[index]
    assert metric.value == pytest.approx(expected)
    assert (metric.numerator, metric.denominator) == (numerator, denominator)


def test_empty_and_unlabelled_are_unknown_not_zero() -> None:
    assert all(m.value is None and m.denominator == 0 for m in calculate(()).metrics)
    values = calculate((MetricSample(task_id=uuid4(), alert=True),)).metrics
    assert values[0].value is values[5].value is values[6].value is None


def test_duplicate_task_rejected() -> None:
    sample = MetricSample(task_id=uuid4())
    with pytest.raises(ValueError, match="重复"):
        calculate((sample, sample))


@pytest.mark.parametrize(
    "data",
    [
        {"approval_rejections": 1},
        {"verification_failures": 1},
        {"runbook_hit": True},
        {"execution_verified": True},
        {"false_alert": True},
        {"mttr_seconds": -1.0},
        {"mttr_seconds": float("nan")},
        {"tool_calls": -1},
    ],
)
def test_inconsistent_or_invalid_measurements_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MetricSample.model_validate({"task_id": uuid4(), **data})


def test_explicit_aliases_and_no_self_scoring() -> None:
    label = EvaluationLabel(accepted_root_causes=("DB Pool", "连接池耗尽"), evidence_ids=(uuid4(),))
    assert root_cause_hit(" db   pool ", label) is True
    assert root_cause_hit("网络故障", label) is False
    assert root_cause_hit("DB Pool", None) is None


def test_backdated_evidence_and_late_audit_are_not_available() -> None:
    now = utc_now()
    record = Evidence(
        id=uuid4(),
        task_id=uuid4(),
        source_tool="query_logs",
        parameters={},
        result_snapshot={},
        collected_at=now - timedelta(days=1),
        created_at=now,
    )
    cutoff = now - timedelta(hours=1)
    assert not available(record, cutoff)
    audit = AuditRecord(
        id=uuid4(),
        task_id=record.task_id,
        evidence_id=record.id,
        event_type=AuditEventType.TOOL_CALL,
        operation="query_logs",
        actor="codex-main-agent",
        outcome="succeeded",
        details={"mode": "live"},
        occurred_at=now - timedelta(days=1),
        created_at=now,
    )
    assert not successful(audit, record, cutoff)
    assert successful(audit, record, now)


@pytest.mark.asyncio
async def test_replay_registry_has_no_write_or_live_handler() -> None:
    registry = replay_registry()
    assert "execute_action" not in {d.name for d in registry.declarations()}
    tool = registry._get("get_service_context")
    query, _ = tool.prepare({"service_name": "payment-service"})
    with pytest.raises(RuntimeError, match="禁止"):
        await tool.invoke(query)


def test_window_utc_and_boundaries() -> None:
    now = utc_now()
    with pytest.raises(ValidationError):
        EvaluationWindow(start=now, end=now)
    with pytest.raises(ValidationError):
        EvaluationWindow(start=now.replace(tzinfo=None), end=now + timedelta(hours=1))
