"""禁真实网络：六类判据、方向/边界、成功重置和幂等预算。"""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.tasks.safety.engine import evaluate
from app.tasks.safety.models import AbortReason, AutomationAborted, SafetyConfig
from app.tasks.safety.scenario import audit, case_facts, metric_fact, trace_fact
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus
from tests.test_tasks import loaded_task

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


@pytest.mark.parametrize("reason", list(AbortReason))
def test_six_independent_conditions(reason: AbortReason) -> None:
    evidence, audits = case_facts(reason)
    result = evaluate(evidence, audits, SafetyConfig())
    assert {r.reason for r in result} == {reason}
    assert all(r.evidence_ids or r.audit_ids for r in result)


@pytest.mark.parametrize(
    "metric,values",
    [("http_p99_ms", (600.0, 700.0, 800.0)), ("http_success_ratio", (0.98, 0.94, 0.90))],
)
def test_p99_and_success_have_opposite_deterioration_direction(
    metric: str, values: tuple[float, ...]
) -> None:
    item = metric_fact(values, metric=metric)
    assert (
        evaluate([item], [audit(item.source, "succeeded", item)], SafetyConfig())[0].reason
        is AbortReason.METRICS_WORSENING
    )


@pytest.mark.parametrize(
    "values", [(0.09, 0.05, 0.02), (0.05, 0.05, 0.05), (0.0, 0.001, 0.002), (0.02, 0.05)]
)
def test_healthy_recovering_flat_or_insufficient_metrics_do_not_abort(
    values: tuple[float, ...],
) -> None:
    item = metric_fact(values)
    assert evaluate([item], [audit(item.source, "succeeded", item)], SafetyConfig()) == ()


def test_metrics_require_success_audit_and_same_series_nonoverlapping_windows() -> None:
    first, second = metric_fact((0.02,) * 3), metric_fact((0.09,) * 3, offset=3)
    assert evaluate([first, second], [], SafetyConfig()) == ()
    audits = [audit(e.source, "succeeded", e) for e in (first, second)]
    assert evaluate([first, second], audits, SafetyConfig())
    # 乱序入库不改变按源采样时间计算的结果。
    assert evaluate([second, first], audits, SafetyConfig()) == evaluate(
        [first, second], audits, SafetyConfig()
    )
    overlapping = replace(second, parameters=first.parameters, result=first.result)
    assert evaluate([first, overlapping], audits, SafetyConfig()) == ()


def test_impact_requires_expansion_in_later_nonoverlapping_window() -> None:
    first, same = trace_fact(("payment-service",)), trace_fact(("payment-service",), offset=3)
    assert not evaluate(
        [first, same], [audit(e.source, "succeeded", e) for e in (first, same)], SafetyConfig()
    )
    replacement = trace_fact(("checkout-service",), offset=3)
    assert not evaluate(
        [first, replacement],
        [audit(e.source, "succeeded", e) for e in (first, replacement)],
        SafetyConfig(),
    )


def test_success_resets_operation_failures_and_replay_or_rejection_does_not_count() -> None:
    _, failures = case_facts(AbortReason.EXECUTION_FAILURES)
    success = audit("execute_action", "succeeded", index=3)
    assert evaluate([], [*failures, success], SafetyConfig()) == ()
    replay = [replace(a, details={"mode": "replay"}) for a in failures]
    assert evaluate([], replay, SafetyConfig()) == ()
    assert evaluate([], [replace(a, outcome="rejected") for a in failures], SafetyConfig()) == ()


def test_exact_action_limit_and_duplicate_intents_do_not_count_twice() -> None:
    items, _ = case_facts(AbortReason.ACTION_LIMIT)
    config = SafetyConfig(max_actions=3)
    assert evaluate(items[:3], [], config) == ()
    assert evaluate([*items[:3], items[0]], [], config) == ()
    assert evaluate(items[:3], [], config, pending_action=("fake-plan", "action-0")) == ()
    assert (
        evaluate(items[:3], [], config, pending_action=("fake-plan", "action-3"))[0].reason
        is AbortReason.ACTION_LIMIT
    )


def test_runbook_multiple_failed_steps_in_one_attempt_count_once() -> None:
    items, audits = case_facts(AbortReason.RUNBOOK_FAILURES)
    duplicate = replace(items[-1], parameters=items[1].parameters)
    assert evaluate([items[0], items[1], duplicate], audits, SafetyConfig()) == ()


def test_large_sample_gaps_do_not_prove_continuous_worsening() -> None:
    item = metric_fact((0.02, 0.05, 0.09))
    assert not evaluate(
        [item], [audit(item.source, "succeeded", item)], SafetyConfig(max_sample_gap_seconds=30)
    )


def test_safety_config_environment_only_and_invalid_thresholds_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SAFETY_CONFIG", '{"max_actions":2}')
    assert Settings().safety_config.max_actions == 2
    for value in (0, -1, True):
        with pytest.raises(ValidationError):
            SafetyConfig.model_validate({"max_actions": value})


def test_window_mismatch_cannot_prove_deterioration() -> None:
    item = metric_fact((0.02, 0.05, 0.09))
    changed = dict(item.parameters, end=(item.collected_at + timedelta(hours=1)).isoformat())
    assert not evaluate(
        [replace(item, parameters=changed)], [audit(item.source, "succeeded", item)], SafetyConfig()
    )


@pytest.mark.asyncio
async def test_abort_latch_blocks_execution_state_before_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = loaded_task(TaskStatus.PLANNING)
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = True
    session.scalar.return_value = task
    monkeypatch.setattr("app.tasks.takeover.takeover_record", AsyncMock(return_value=None))
    monkeypatch.setattr("app.tasks.safety.service.abort_record", AsyncMock(return_value=object()))
    with pytest.raises(AutomationAborted):
        await TaskService(session).transition(
            task.id,
            TaskStatus.EXECUTING,
            expected_status=task.status,
            expected_version=task.status_version,
            reason="旧审批尝试恢复",
        )
    assert task.status is TaskStatus.PLANNING and session.add.call_count == 0
