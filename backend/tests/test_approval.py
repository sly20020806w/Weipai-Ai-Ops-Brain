"""禁真实网络的审批身份、完整动作哈希、卡片和信号隔离验收。"""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from temporalio.converter import DataConverter

from app.connectors.feishu.fake import FakeFeishuConnector
from app.tasks.approval.models import ApprovalTicket, action_hash, validate_response
from app.tasks.approval.service import approval_card, make_ticket, prompt_for
from app.tasks.planning.models import ActionPlan
from app.tasks.states import TaskStatus
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import (
    ApprovalDecisionRequest,
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalResult,
    HumanResponse,
    TaskSnapshot,
)
from tests.test_action_plans import draft, evaluate

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def sample() -> tuple[ActionPlan, ApprovalTicket, ApprovalPrompt]:
    plan = evaluate(draft())
    request = ApprovalRequest(
        TaskSnapshot(str(plan.task_id), TaskStatus.WAITING_APPROVAL, plan.planning_version + 1),
        str(uuid4()),
    )
    ticket = make_ticket(request, plan)
    return plan, ticket, prompt_for(ticket, uuid4())


def response(prompt: ApprovalPrompt, decision: str = "approved") -> ApprovalResponse:
    return ApprovalResponse(
        prompt.task.task_id,
        prompt.approval_id,
        prompt.task.version,
        prompt.action_hash,
        decision,
        "owner",
    )


@pytest.mark.parametrize(
    "field", ["parameters", "service_name", "risk_level", "rollback", "verification", "environment"]
)
def test_changed_action_invalidates_hash(field: str) -> None:
    plan, _, _ = sample()
    data = plan.model_dump(mode="json")
    action = data["actions"][0]["action"]
    if field == "environment":
        data["environment"] = "production"
    elif field == "parameters":
        action["parameters"]["to_version"] = "v2.3.5"
    elif field == "rollback":
        action["rollback"]["parameters"]["to_version"] = "v2.3.5"
    elif field == "verification":
        action["verification"]["success_criteria"] = "改动验证标准"
    else:
        action[field] = "checkout-service" if field == "service_name" else "L4"
        if field == "risk_level":
            data["actions"][0]["policy"]["risk_level"] = "L4"
    # 环境也须在逐项 Policy 中一致；完整 JSON 的验证由 ActionPlan 负责。
    changed = ActionPlan.model_validate_json(json.dumps(data))
    assert action_hash(changed) != action_hash(plan)


def test_hash_survives_jsonb_key_reordering() -> None:
    plan, _, _ = sample()
    data = json.loads(json.dumps(plan.model_dump(mode="json"), sort_keys=True))
    parameters = data["actions"][0]["action"]["parameters"]
    data["actions"][0]["action"]["parameters"] = dict(reversed(list(parameters.items())))
    assert action_hash(ActionPlan.model_validate_json(json.dumps(data))) == action_hash(plan)


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": "bad"},
        {"approval_id": str(uuid4()).upper()},
        {"wait_version": True},
        {"wait_version": 0},
        {"action_hash": "bad"},
        {"decision": True},
        {"decision": "expired"},
        {"actor": " "},
        {"actor": "x" * 201},
    ],
)
def test_invalid_decision_rejected(changes: dict[str, object]) -> None:
    _, _, prompt = sample()
    with pytest.raises((TypeError, ValueError)):
        validate_response(replace(response(prompt), **changes))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fake_card_has_reviewable_parameters_and_bound_buttons() -> None:
    plan, ticket, prompt = sample()
    card = approval_card(ticket)
    connector = FakeFeishuConnector()
    receipt = await connector.send(card)
    assert await connector.send(card) == receipt
    assert connector.get_sent(ticket.approval_id).notification == card
    assert len(connector.sent_messages) == 1
    assert "v2.3.7" in card.card.markdown and "v2.3.6" in card.card.markdown
    assert "回滚" in card.card.markdown and "验证" in card.card.markdown
    for button in card.card.buttons:
        assert button.value["action_hash"] == action_hash(plan)
        assert button.value["approval_id"] == prompt.approval_id


@pytest.mark.parametrize("field", ["task_id", "approval_id", "wait_version", "action_hash"])
def test_signal_ignores_old_wrong_and_cross_task_decisions(field: str) -> None:
    _, _, prompt = sample()
    instance = AITaskWorkflow()
    instance.task, instance.approval_prompt, instance.approval_active = prompt.task, prompt, True
    wrong = {
        field: prompt.task.version - 1
        if field == "wait_version"
        else "0" * 64
        if field == "action_hash"
        else str(uuid4())
    }
    instance.approve_actions(replace(response(prompt), **wrong))  # type: ignore[arg-type]
    assert instance.approval_response is None


def test_boolean_cannot_approve_and_first_valid_decision_wins() -> None:
    _, _, prompt = sample()
    instance = AITaskWorkflow()
    instance.task, instance.approval_prompt, instance.approval_active = prompt.task, prompt, True
    instance.human_response(HumanResponse(prompt.task.status, prompt.task.version, True))
    assert instance.response is None
    instance.approve_actions(response(prompt, "rejected"))
    instance.approve_actions(response(prompt))
    assert instance.approval_response == response(prompt, "rejected")
    instance.approval_response, instance.approval_active = None, False
    instance.approve_actions(response(prompt))
    assert instance.approval_response is None


@pytest.mark.asyncio
async def test_approval_temporal_payload_roundtrip() -> None:
    _, _, prompt = sample()
    converter = DataConverter.default
    for value in [
        prompt,
        response(prompt),
        ApprovalRequest(prompt.task, prompt.plan_evidence_id),
        ApprovalDecisionRequest(prompt, response(prompt)),
        ApprovalDecisionRequest(prompt),
        ApprovalResult(str(uuid4()), "approved", prompt.action_hash),
    ]:
        assert (await converter.decode(await converter.encode([value]), [type(value)]))[0] == value
