"""Action Plan schema、风险下限、证据与逐项 Policy 的禁联网验收。"""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.investigation import AgentConclusion, EvidenceClaim
from app.agent.models import ChatMessage, ChatResponse, FunctionCall, ToolCall
from app.agent.reviewer.models import AlternativeCause, ReviewCheck, ReviewDecision, ReviewReport
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyConfig, PolicyDecision, PolicyEnvironment, RiskLevel
from app.tasks.planning.engine import evaluate_plan, parse_draft, planning_chat
from app.tasks.planning.models import ActionPlan, ActionPlanDraft, PlanningRequest
from app.tasks.planning.scenario import payment_plan_response
from app.tasks.states import TaskStatus
from app.tasks.workflow_models import TaskSnapshot
from tests.test_main_agent import SPEC

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def response() -> ChatResponse:
    reference = uuid4()
    claim = EvidenceClaim(statement="连接池与发布线索", evidence_ids=(reference,))
    conclusion = AgentConclusion(root_cause=claim, findings=(claim,), confidence=0.8)
    review = ReviewDecision(
        conclusion_evidence_id=uuid4(),
        original_confidence=0.7,
        conclusion=conclusion,
        report=ReviewReport(
            checks=tuple(
                ReviewCheck(
                    statement="未找到反证",
                    evidence_ids=(reference,),
                    alternative=kind,
                    outcome="not_supported",
                )
                for kind in AlternativeCause
            )
        ),
        observed_ids=(reference,),
        steps=5,
    )
    return payment_plan_response(planning_chat(SPEC, review, []))


def draft() -> ActionPlanDraft:
    return parse_draft(response())


def request() -> PlanningRequest:
    return PlanningRequest(
        TaskSnapshot(str(uuid4()), TaskStatus.PLANNING, 5),
        SPEC.model_dump_json(),
        str(uuid4()),
        str(uuid4()),
    )


def evaluate(value: ActionPlanDraft, policy: PolicyEngine | None = None) -> ActionPlan:
    return evaluate_plan(
        value,
        request(),
        SPEC,
        frozenset(value.summary.evidence_ids),
        policy or PolicyEngine(PolicyEnvironment.TEST),
    )


def test_payment_rollback_has_required_fields_and_needs_approval() -> None:
    plan = evaluate(draft())
    action = plan.actions[0]
    assert action.action.parameters == {"from_version": "v2.3.7", "to_version": "v2.3.6"}
    assert action.action.service_name == "payment-service"
    assert action.action.risk_level is RiskLevel.L3
    assert action.policy.decision is plan.decision is PolicyDecision.NEED_APPROVAL
    assert action.action.rollback.description and action.action.verification.checks


@pytest.mark.parametrize(
    "field", ["rollback", "verification", "parameters", "rationale", "preconditions"]
)
def test_missing_required_action_field_cannot_enter_plan(field: str) -> None:
    data = json.loads(draft().model_dump_json())
    del data["actions"][0][field]
    with pytest.raises(ValidationError):
        ActionPlanDraft.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("field", ["rollback", "verification", "parameters", "preconditions"])
def test_empty_required_action_field_cannot_enter_plan(field: str) -> None:
    data = json.loads(draft().model_dump_json())
    data["actions"][0][field] = [] if field == "preconditions" else {}
    with pytest.raises(ValidationError):
        ActionPlanDraft.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("risk", list(RiskLevel))
def test_model_cannot_lower_rollback_risk(risk: RiskLevel) -> None:
    value = draft()
    value = value.model_copy(
        update={"actions": (value.actions[0].model_copy(update={"risk_level": risk}),)}
    )
    level = evaluate(value).actions[0].policy.risk_level
    assert int(level.value[1]) == max(3, int(risk.value[1]))


@pytest.mark.parametrize("mode", ["missing", "null", "unknown"])
def test_missing_null_or_unknown_action_is_l5(mode: str) -> None:
    data = json.loads(draft().model_dump_json())
    if mode == "missing":
        del data["actions"][0]["risk_level"]
    elif mode == "null":
        data["actions"][0]["risk_level"] = None
    else:
        data["actions"][0].update(name="unrecognized_action", risk_level="L0")
    assert (
        evaluate(ActionPlanDraft.model_validate_json(json.dumps(data))).actions[0].policy.risk_level
        is RiskLevel.L5
    )


def test_each_action_gets_policy_and_strictest_decision_wins() -> None:
    value = draft()
    value = value.model_copy(
        update={
            "actions": (
                value.actions[0],
                value.actions[0].model_copy(update={"id": "second", "name": "unknown_action"}),
            )
        }
    )
    policy = PolicyEngine(
        PolicyEnvironment.PRODUCTION,
        PolicyConfig.model_validate(
            {
                "rules": [
                    {
                        "id": "block-l5",
                        "risk_levels": ["L5"],
                        "decision": "deny",
                        "reason": "未知动作禁止",
                    }
                ]
            }
        ),
    )
    plan = evaluate(value, policy)
    assert [item.policy.decision for item in plan.actions] == [
        PolicyDecision.NEED_APPROVAL,
        PolicyDecision.DENY,
    ]
    assert plan.decision is PolicyDecision.DENY
    assert all(item.policy.environment is PolicyEnvironment.PRODUCTION for item in plan.actions)


@pytest.mark.parametrize(
    "mode", ["evidence", "service", "duplicate", "same_version", "extra_parameter"]
)
def test_forged_evidence_scope_and_invalid_parameters_rejected(mode: str) -> None:
    value = draft()
    data = json.loads(value.model_dump_json())
    if mode == "evidence":
        data["actions"][0]["rationale"]["evidence_ids"] = [str(uuid4())]
    elif mode == "service":
        data["actions"][0]["service_name"] = "checkout-service"
    elif mode == "duplicate":
        data["actions"].append(data["actions"][0])
    elif mode == "same_version":
        data["actions"][0]["parameters"]["to_version"] = "v2.3.7"
    else:
        data["actions"][0]["parameters"]["service_name"] = "checkout-service"
    with pytest.raises(ValueError):
        evaluate_plan(
            ActionPlanDraft.model_validate_json(json.dumps(data)),
            request(),
            SPEC,
            frozenset(value.summary.evidence_ids),
            PolicyEngine(PolicyEnvironment.TEST),
        )


@pytest.mark.parametrize("mode", ["truncated", "refusal", "tools"])
def test_planner_cannot_call_tools_or_accept_incomplete_output(mode: str) -> None:
    value = response()
    if mode == "truncated":
        value = value.model_copy(update={"finish_reason": "length"})
    elif mode == "refusal":
        value = value.model_copy(update={"message": ChatMessage(role="assistant", refusal="拒绝")})
    else:
        value = value.model_copy(
            update={
                "message": ChatMessage(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            id="write", function=FunctionCall(name="execute_action", arguments="{}")
                        ),
                    ),
                )
            }
        )
    with pytest.raises(ValueError):
        parse_draft(value)


def test_spec_environment_is_owned_by_host() -> None:
    plan = evaluate(draft(), PolicyEngine(PolicyEnvironment.LOCAL))
    assert plan.environment is PolicyEnvironment.LOCAL
