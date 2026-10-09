"""主 Agent 纯循环验收：真实观察引用、协议完整性、预算与拒绝处理。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from temporalio.converter import DataConverter

from app.agent.config import AgentConfig
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import (
    AgentConclusion,
    AgentStepLimit,
    InvalidConclusion,
    InvestigationResult,
    InvestigationSpec,
    MainAgent,
)
from app.agent.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FunctionCall,
    ToolCall,
    ToolDefinition,
)
from app.agent.scenario import PAYMENT_TOOLS, payment_llm
from app.agent.workflow_models import ConclusionRequest, InvestigationRequest
from app.config import Settings
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyAction, PolicyEnvironment, RiskLevel
from app.tasks.states import TaskStatus
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import TaskSnapshot, WorkflowInput
from app.tools.models import DispatchResult, DispatchStatus

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("forbid_llm_network")]
END = datetime(2026, 10, 1, 2, tzinfo=UTC)
SPEC = InvestigationSpec(
    service_name="payment-service",
    title="payment-service 5xx 升高",
    start=END - timedelta(hours=1),
    end=END,
)


class MemoryIO:
    definitions: tuple[ToolDefinition, ...] = ()

    def __init__(self, llm: FakeLLM, status: DispatchStatus = DispatchStatus.SUCCEEDED) -> None:
        self.llm, self.status = llm, status
        self.tool_names: list[str] = []
        self.ids: list[str] = []

    async def think(self, step: int, request: ChatRequest) -> ChatResponse:
        return await self.llm.chat(request)

    async def call(self, step: int, call: ToolCall) -> DispatchResult:
        self.tool_names.append(call.function.name)
        evidence_id = uuid4() if self.status is DispatchStatus.SUCCEEDED else None
        if evidence_id is not None:
            self.ids.append(str(evidence_id))
        return DispatchResult(
            status=self.status,
            policy=PolicyEngine(PolicyEnvironment.TEST).evaluate(
                PolicyAction(name=call.function.name, risk_level=RiskLevel.L0)
            ),
            audit_id=uuid4(),
            evidence_id=evidence_id,
            result={"nodes": [{}], "events": [{}], "series": [{}], "logs": [{}]},
            error_code=None if evidence_id else "policy_denied",
        )


async def test_scripted_payment_loop_observes_actual_ids() -> None:
    io = MemoryIO(payment_llm())
    result = await MainAgent().run(SPEC, io)
    assert io.tool_names == list(PAYMENT_TOOLS)
    assert result.steps == 9 and result.observed_ids == io.ids
    assert AgentConclusion.model_validate_json(result.conclusion_json).evidence_ids == {
        UUID(value) for value in io.ids
    }
    final_request = io.llm.calls[-1]
    assert isinstance(final_request, ChatRequest)
    assert [
        message.tool_call_id for message in final_request.messages if message.role == "tool"
    ] == [f"payment-query-{index}" for index in range(4)]


async def test_nonexistent_evidence_rejected() -> None:
    io = MemoryIO(payment_llm("invalid"))
    with pytest.raises(InvalidConclusion, match="未观察"):
        await MainAgent().run(SPEC, io)


@pytest.mark.parametrize("budget,expected_calls", [(1, 0), (2, 1), (3, 1), (8, 4)])
async def test_global_budget_counts_think_and_each_tool(budget: int, expected_calls: int) -> None:
    io = MemoryIO(payment_llm())
    with pytest.raises(AgentStepLimit):
        await MainAgent().run(SPEC.model_copy(update={"max_steps": budget}), io)
    assert len(io.tool_names) == expected_calls


@pytest.mark.parametrize("status", [DispatchStatus.REJECTED, DispatchStatus.FAILED])
async def test_failed_and_denied_queries_never_become_evidence(status: DispatchStatus) -> None:
    io = MemoryIO(payment_llm(), status)
    with pytest.raises(InvalidConclusion, match="拒绝"):
        await MainAgent().run(SPEC, io)
    assert io.ids == []


@pytest.mark.parametrize(
    "message,finish",
    [
        (ChatMessage(role="assistant", content="不是结构化结论"), "stop"),
        (ChatMessage(role="assistant", content="{}"), "stop"),
        (ChatMessage(role="assistant", content="{}"), "length"),
        (ChatMessage(role="assistant", refusal="拒绝"), "stop"),
    ],
)
async def test_invalid_or_truncated_final_response_is_rejected(
    message: ChatMessage,
    finish: str,
) -> None:
    llm = FakeLLM(
        [
            ScriptedChatStep(
                lambda request: ChatResponse(
                    id="invalid", model="fake", message=message, finish_reason=finish
                )
            )
        ]
    )
    with pytest.raises(InvalidConclusion):
        await MainAgent().run(SPEC, MemoryIO(llm))


async def test_multi_tool_response_cannot_bypass_budget() -> None:
    calls = tuple(
        ToolCall(id=f"call-{index}", function=FunctionCall(name="query_logs", arguments="{}"))
        for index in range(10)
    )
    llm = FakeLLM(
        [
            ScriptedChatStep(
                lambda request: ChatResponse(
                    id="multi",
                    model="fake",
                    finish_reason="tool_calls",
                    message=ChatMessage(role="assistant", tool_calls=calls),
                )
            )
        ]
    )
    io = MemoryIO(llm)
    with pytest.raises(AgentStepLimit):
        await MainAgent().run(SPEC.model_copy(update={"max_steps": 3}), io)
    assert len(io.tool_names) == 2


async def test_reused_tool_call_id_is_rejected() -> None:
    call = ToolCall(id="reused", function=FunctionCall(name="query_logs", arguments="{}"))

    def response(request: ChatRequest) -> ChatResponse:
        return ChatResponse(
            id="duplicate",
            model="fake",
            finish_reason="tool_calls",
            message=ChatMessage(role="assistant", tool_calls=(call,)),
        )

    io = MemoryIO(FakeLLM([ScriptedChatStep(response), ScriptedChatStep(response)]))
    with pytest.raises(InvalidConclusion, match="重复"):
        await MainAgent().run(SPEC, io)
    assert len(io.tool_names) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"max_steps": 0},
        {"max_steps": 101},
        {"max_steps": True},
        {"start": END},
        {"start": END.replace(tzinfo=None)},
        {"start": END - timedelta(days=2)},
        {"service_name": "bad/service"},
    ],
)
async def test_invalid_investigation_scope(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InvestigationSpec.model_validate(SPEC.model_dump() | values)


async def test_environment_config_and_workflow_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AGENT_CONFIG", '{"enabled":true,"max_steps":12}')
    assert Settings().agent_config == AgentConfig(enabled=True, max_steps=12)
    task = TaskSnapshot(str(uuid4()), TaskStatus.INVESTIGATING, 3)
    value = WorkflowInput(task.task_id, investigation_json=SPEC.model_dump_json())
    validate_workflow_input(value)
    with pytest.raises(ValueError, match="混用"):
        validate_workflow_input(replace(value, waits=[TaskStatus.WAITING_INFORMATION]))
    for request in (
        value,
        InvestigationRequest(task, SPEC.model_dump_json()),
        ConclusionRequest(task, InvestigationResult("{}", [], 1)),
    ):
        converter = DataConverter.default
        assert (await converter.decode(await converter.encode([request]), [type(request)]))[
            0
        ] == request


async def test_every_claim_requires_evidence() -> None:
    io = MemoryIO(payment_llm())
    result = await MainAgent().run(SPEC, io)
    data = json.loads(result.conclusion_json)
    data["findings"][0]["evidence_ids"] = []
    with pytest.raises(ValidationError):
        AgentConclusion.model_validate_json(json.dumps(data))
