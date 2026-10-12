"""验收门槛不会因零样本、未标注、失败或重复回放而误放行。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.investigation import AgentConclusion, EvidenceClaim
from app.learning.evaluation.acceptance import AcceptanceCriteria, evaluate_benchmark
from app.learning.evaluation.models import ReplayReport


def report(*, hit: bool | None = True, completed: bool = True) -> ReplayReport:
    evidence_id = uuid4()
    return ReplayReport(
        run_id=uuid4(),
        task_id=uuid4(),
        candidate_version="offline-payment-v1",
        cutoff=datetime.now(UTC),
        baseline_evidence_id=uuid4(),
        candidate=(
            AgentConclusion(
                root_cause=EvidenceClaim(statement="样例根因", evidence_ids=(evidence_id,)),
                findings=(EvidenceClaim(statement="样例事实", evidence_ids=(evidence_id,)),),
                confidence=0.7,
                uncertainties=(),
            )
            if completed
            else None
        ),
        baseline_root_cause="样例根因",
        baseline_hit=True,
        candidate_hit=hit,
        misjudged=None if hit is None else not hit,
        label_evidence_id=uuid4() if hit is not None else None,
        baseline_tool_calls=4,
        candidate_tool_calls=4,
        baseline_elapsed_seconds=1,
        candidate_elapsed_seconds=1,
        observed_evidence_ids=(evidence_id,),
        status="completed" if completed else "incomplete",
        error_code=None if completed else "historical_result_unavailable",
    )


def test_hit_rate_boundary_and_all_samples_count() -> None:
    samples = (report(), report(), report(hit=False))
    result = evaluate_benchmark(samples, AcceptanceCriteria(rca_hit_rate=2 / 3))
    assert result.passed and result.hits == 2 and result.total == 3
    assert not evaluate_benchmark(samples, AcceptanceCriteria(rca_hit_rate=0.67)).passed


@pytest.mark.parametrize(
    "samples",
    [
        (),
        (report(),),
        (report(), report(hit=None), report()),
        (report(), report(completed=False), report()),
    ],
)
def test_insufficient_unknown_and_incomplete_fail(samples: tuple[ReplayReport, ...]) -> None:
    assert not evaluate_benchmark(samples, AcceptanceCriteria(rca_hit_rate=0.5)).passed


def test_duplicate_incident_and_mixed_candidate_rejected() -> None:
    first = report()
    with pytest.raises(ValueError, match="不同事故"):
        evaluate_benchmark(
            (first, first.model_copy(update={"run_id": uuid4()})), AcceptanceCriteria()
        )
    with pytest.raises(ValueError, match="候选版本"):
        evaluate_benchmark(
            (first, report().model_copy(update={"candidate_version": "other"})),
            AcceptanceCriteria(),
        )


@pytest.mark.parametrize("threshold", [0, -0.1, 1.1, float("nan"), float("inf")])
def test_invalid_threshold_rejected(threshold: float) -> None:
    with pytest.raises(ValidationError):
        AcceptanceCriteria(rca_hit_rate=threshold)
