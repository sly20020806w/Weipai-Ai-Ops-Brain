"""成熟度规则、审核门禁和风险矩阵；禁止真实网络。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.policy.engine import PolicyEngine
from app.policy.models import (
    PolicyAction,
    PolicyConfig,
    PolicyDecision,
    PolicyEnvironment,
    PolicyRule,
    RiskLevel,
    RunbookPolicyContext,
)
from app.runbooks.maturity import MaturityConfig, MaturityState, record_result
from app.runbooks.schemas import RunbookMaturity

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def test_default_thresholds_promote_one_stage_at_a_time_after_human_review() -> None:
    state = MaturityState(maturity=RunbookMaturity.REVIEWED, review_evidence_id=uuid4())
    observed = {}
    for trial in range(1, 21):
        state = record_result(state, True, MaturityConfig())
        if trial in {2, 3, 5, 10, 20}:
            observed[trial] = state.maturity
    assert observed == {
        2: RunbookMaturity.REVIEWED,
        3: RunbookMaturity.VERIFIED,
        5: RunbookMaturity.SEMI_AUTOMATED,
        10: RunbookMaturity.APPROVAL_AUTOMATED,
        20: RunbookMaturity.SELF_HEALING,
    }
    assert state.success_count == 20 and state.failure_count == 0
    assert state.confidence == pytest.approx(21 / 22)


def test_successes_without_review_never_promote_and_confidence_is_not_model_supplied() -> None:
    state = MaturityState()
    for _ in range(30):
        state = record_result(state, True, MaturityConfig())
    assert state.maturity is RunbookMaturity.DRAFT
    state = record_result(state, False, MaturityConfig())
    assert state.confidence == pytest.approx(31 / 33)


def test_first_failure_revokes_self_healing_and_consecutive_failure_requires_new_review() -> None:
    state = MaturityState(
        success_count=20, maturity=RunbookMaturity.SELF_HEALING, review_evidence_id=uuid4()
    )
    state = record_result(state, False, MaturityConfig())
    assert state.maturity is RunbookMaturity.APPROVAL_AUTOMATED
    state = record_result(state, False, MaturityConfig())
    assert state.maturity is RunbookMaturity.REVIEWED and state.review_evidence_id is None
    assert state.failure_count == 2 and state.consecutive_failures == 2
    assert record_result(state, True, MaturityConfig()).maturity is RunbookMaturity.DRAFT


def test_success_resets_failure_streak_but_retains_lifetime_counts() -> None:
    state = MaturityState(review_evidence_id=uuid4(), maturity=RunbookMaturity.REVIEWED)
    for passed in (False, True, False):
        state = record_result(state, passed, MaturityConfig())
    assert (state.success_count, state.failure_count, state.consecutive_failures) == (1, 2, 1)
    assert state.confidence == pytest.approx(2 / 5)


@pytest.mark.parametrize("maturity", list(RunbookMaturity))
@pytest.mark.parametrize("risk", list(RiskLevel))
def test_explicit_allow_cannot_bypass_maturity_or_high_risk_approval(
    maturity: RunbookMaturity,
    risk: RiskLevel,
) -> None:
    config = PolicyConfig(
        rules=(
            PolicyRule(
                id="explicit",
                risk_levels=tuple(RiskLevel),
                decision=PolicyDecision.ALLOW,
                reason="验收规则",
            ),
        )
    )
    context = RunbookPolicyContext(
        runbook_id=uuid4(),
        revision="a" * 64,
        maturity=maturity.value,
        trusted=True,
        review_evidence_id=uuid4(),
        state_evidence_id=uuid4(),
    )
    result = PolicyEngine(PolicyEnvironment.TEST, config).evaluate(
        PolicyAction(name="sample_action", risk_level=risk, runbook=context)
    )
    allowed = risk is RiskLevel.L0 or (
        maturity is RunbookMaturity.SELF_HEALING and risk in {RiskLevel.L1, RiskLevel.L2}
    )
    assert result.decision is (PolicyDecision.ALLOW if allowed else PolicyDecision.NEED_APPROVAL)


@pytest.mark.parametrize("decision", [PolicyDecision.NEED_APPROVAL, PolicyDecision.DENY])
def test_maturity_never_overrides_stricter_policy(decision: PolicyDecision) -> None:
    policy = PolicyEngine(
        PolicyEnvironment.PRODUCTION,
        PolicyConfig(
            rules=(
                PolicyRule(
                    id="strict", risk_levels=(RiskLevel.L1,), decision=decision, reason="严格规则"
                ),
            )
        ),
    )
    context = RunbookPolicyContext(
        runbook_id=uuid4(),
        revision="b" * 64,
        maturity="self_healing",
        trusted=True,
        review_evidence_id=uuid4(),
        state_evidence_id=uuid4(),
    )
    assert (
        policy.evaluate(
            PolicyAction(name="sample", risk_level=RiskLevel.L1, runbook=context)
        ).decision
        is decision
    )


@pytest.mark.parametrize("field", ["trusted", "review_evidence_id", "state_evidence_id"])
def test_self_healing_label_alone_cannot_authorize(field: str) -> None:
    context = RunbookPolicyContext(
        runbook_id=uuid4(),
        revision="c" * 64,
        maturity="self_healing",
        trusted=True,
        review_evidence_id=uuid4(),
        state_evidence_id=uuid4(),
    ).model_copy(update={field: False if field == "trusted" else None})
    policy = PolicyEngine(
        PolicyEnvironment.TEST,
        PolicyConfig(
            rules=(
                PolicyRule(
                    id="allow",
                    risk_levels=(RiskLevel.L1,),
                    decision=PolicyDecision.ALLOW,
                    reason="允许样例",
                ),
            )
        ),
    )
    assert (
        policy.evaluate(
            PolicyAction(name="sample", risk_level=RiskLevel.L1, runbook=context)
        ).decision
        is PolicyDecision.NEED_APPROVAL
    )


def test_environment_config_and_invalid_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNBOOK_MATURITY_CONFIG", '{"verified_successes":2}')
    assert Settings(APP_ENV="test").runbook_maturity_config.verified_successes == 2
    for data in (
        {"verified_successes": 5},
        {"consecutive_failure_limit": 0},
        {"min_confidence": 0.99, "self_healing_confidence": 0.8},
    ):
        with pytest.raises(ValidationError):
            MaturityConfig.model_validate(data)
