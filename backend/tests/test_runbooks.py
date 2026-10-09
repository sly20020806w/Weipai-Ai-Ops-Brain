"""Step 25 严格字段、条件判定和 Runbook 诊断；禁止真实网络。"""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import AgentConclusion, AgentStepLimit, InvalidConclusion, MainAgent
from app.agent.models import ToolCall, ToolDefinition, ToolFunction
from app.agent.scenario import PAYMENT_TOOLS, conclusion_response
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import (
    MatchingFacts,
    RunbookDraft,
    RunbookView,
    applicability,
)
from app.tools.models import DispatchResult
from tests.test_main_agent import SPEC, MemoryIO

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


class RunbookIO(MemoryIO):
    definitions = tuple(
        ToolDefinition(function=ToolFunction(name=name, description="Fake 只读查询", parameters={}))
        for name in PAYMENT_TOOLS
    )

    def __init__(self) -> None:
        super().__init__(FakeLLM([ScriptedChatStep(conclusion_response)]))
        self.calls: list[ToolCall] = []

    async def call(self, step: int, call: ToolCall) -> DispatchResult:
        self.calls.append(call)
        return await super().call(step, call)


def sample_view(**updates: object) -> RunbookView:
    data = payment_runbook().model_dump(mode="json")
    data.update(updates)
    data.update(
        id=str(uuid4()),
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
        embedding_model="fake",
        embedding_dimensions=3,
    )
    return RunbookView.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("field", list(RunbookDraft.model_fields))
def test_every_runbook_content_field_is_required(field: str) -> None:
    data = payment_runbook().model_dump(mode="json")
    del data[field]
    with pytest.raises(ValidationError, match=field):
        RunbookDraft.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", " "),
        ("rollback_plan", " "),
        ("source", " "),
        ("applicability_conditions", []),
        ("diagnostic_steps", []),
        ("handling_steps", []),
        ("verification_steps", []),
        ("confidence", 1.1),
        ("success_count", -1),
        ("failure_count", -1),
        ("risk_level", "L0"),
    ],
)
def test_incomplete_or_inconsistent_content_is_rejected(field: str, value: object) -> None:
    data = payment_runbook().model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError):
        RunbookDraft.model_validate_json(json.dumps(data))


def test_diagnostic_risk_and_unknown_condition_fields_are_rejected() -> None:
    data = payment_runbook().model_dump(mode="json")
    data["diagnostic_steps"][0]["risk_level"] = "L3"
    with pytest.raises(ValidationError):
        RunbookDraft.model_validate_json(json.dumps(data))
    data = payment_runbook().model_dump(mode="json")
    data["applicability_conditions"][0]["field"] = "approved"
    with pytest.raises(ValidationError):
        RunbookDraft.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "title,service,maturity,allowed,reason",
    [
        ("支付 5xx", "payment-service", "verified", True, "全部满足"),
        ("支付维护 5xx", "payment-service", "verified", False, "排除条件"),
        ("支付正常", "payment-service", "verified", False, "未全部满足"),
        ("支付 5xx", "checkout-service", "verified", False, "未全部满足"),
        ("支付 5xx", "payment-service", "draft", False, "尚未经验证"),
        ("支付 5xx", "payment-service", "reviewed", True, "仅按 L0"),
    ],
)
def test_conditions_and_maturity_fail_closed(
    title: str, service: str, maturity: str, allowed: bool, reason: str
) -> None:
    value, actual = applicability(
        sample_view(maturity=maturity),
        MatchingFacts(service_name=service, title=title, task_source="Alert"),
    )
    assert value is allowed and reason in actual


@pytest.mark.asyncio
async def test_guide_executes_diagnostic_order_through_io_with_real_evidence() -> None:
    io = RunbookIO()
    result = await MainAgent().run(SPEC, io, sample_view())
    assert [call.function.name for call in io.calls] == list(PAYMENT_TOOLS)
    assert all(call.id.startswith("runbook-") for call in io.calls)
    assert result.steps == 5
    assert AgentConclusion.model_validate_json(result.conclusion_json).evidence_ids == {
        UUID(value) for value in io.ids
    }


@pytest.mark.asyncio
async def test_guide_counts_against_agent_step_limit() -> None:
    io = RunbookIO()
    with pytest.raises(AgentStepLimit):
        await MainAgent().run(SPEC.model_copy(update={"max_steps": 2}), io, sample_view())
    assert len(io.calls) == 2


@pytest.mark.asyncio
async def test_guide_cannot_call_unknown_tool() -> None:
    guide = sample_view()
    step = guide.diagnostic_steps[0].model_copy(update={"tool_name": "execute_action"})
    guide = guide.model_copy(update={"diagnostic_steps": (step,)})
    io = RunbookIO()
    with pytest.raises(InvalidConclusion, match="未注册"):
        await MainAgent().run(SPEC, io, guide)
    assert io.calls == []
