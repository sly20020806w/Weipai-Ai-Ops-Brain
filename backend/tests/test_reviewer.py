"""禁真实网络的反证循环测试：覆盖、协议、真实观察引用和预算。"""

import json
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.agent.config import AgentConfig
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import (
    AgentConclusion,
    AgentStepLimit,
    EvidenceClaim,
    InvalidConclusion,
)
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, ToolCall, ToolDefinition
from app.agent.reviewer.engine import ReviewerAgent
from app.agent.reviewer.models import (
    AlternativeCause,
    ReviewCheck,
    ReviewInput,
    ReviewReport,
    adjusted_conclusion,
    within_review_scope,
)
from app.agent.reviewer.scenario import review_response
from app.connectors.observability.reviewer_fake import reviewer_traces
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyAction, PolicyEnvironment, RiskLevel
from app.tools.models import DispatchResult, DispatchStatus
from tests.test_main_agent import SPEC

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("forbid_llm_network")]


def review_input() -> ReviewInput:
    claim = EvidenceClaim(statement="连接池超时是故障线索", evidence_ids=(uuid4(),))
    return ReviewInput(
        spec=SPEC,
        conclusion_evidence_id=uuid4(),
        conclusion=AgentConclusion(root_cause=claim, findings=(claim,), confidence=0.7),
        evidence_snapshots=(),
    )


class ReviewMemoryIO:
    definitions: tuple[ToolDefinition, ...] = ()

    def __init__(
        self,
        mode: Literal["clear", "contradicted"] = "clear",
        llm: FakeLLM | None = None,
        *,
        status: DispatchStatus = DispatchStatus.SUCCEEDED,
    ) -> None:
        self.mode, self.status = mode, status
        self.llm = llm or FakeLLM([ScriptedChatStep(review_response) for _ in range(3)])
        self.calls: list[ToolCall] = []

    async def think(self, step: int, request: ChatRequest) -> ChatResponse:
        return await self.llm.chat(request)

    async def call(self, step: int, call: ToolCall) -> DispatchResult:
        self.calls.append(call)
        return DispatchResult(
            status=self.status,
            policy=PolicyEngine(PolicyEnvironment.TEST).evaluate(
                PolicyAction(name=call.function.name, risk_level=RiskLevel.L0)
            ),
            audit_id=uuid4(),
            evidence_id=uuid4() if self.status is DispatchStatus.SUCCEEDED else None,
            result={"traces": [item.model_dump(mode="json") for item in reviewer_traces(self.mode)]}
            if call.function.name == "query_traces"
            else {"events": [{"kind": "Deploy"}]},
        )


@pytest.mark.parametrize(
    "mode,verdict,confidence", [("clear", "clear", 0.8), ("contradicted", "contradicted", 0.5)]
)
async def test_independent_snapshot_changes_verdict_and_host_confidence(
    mode: Literal["clear", "contradicted"],
    verdict: str,
    confidence: float,
) -> None:
    value, io = review_input(), ReviewMemoryIO(mode)
    report, observed, steps = await ReviewerAgent().run(value, io, max_steps=16)
    assert report.verdict == verdict and steps == 5
    assert report.evidence_ids == {UUID(item) for item in observed}
    assert {item.alternative for item in report.checks} == set(AlternativeCause)
    assert [item.function.name for item in io.calls] == ["query_traces", "get_recent_changes"]
    assert adjusted_conclusion(value.conclusion, report).confidence == confidence
    assert value.conclusion.confidence == 0.7


@pytest.mark.parametrize("budget,calls", [(1, 0), (2, 1), (3, 1), (4, 2)])
async def test_reviewer_counts_llm_and_tool_steps(budget: int, calls: int) -> None:
    io = ReviewMemoryIO()
    with pytest.raises(AgentStepLimit):
        await ReviewerAgent().run(review_input(), io, max_steps=budget)
    assert len(io.calls) == calls


@pytest.mark.parametrize("status", [DispatchStatus.REJECTED, DispatchStatus.FAILED])
async def test_rejected_queries_cannot_prove_exclusion(status: DispatchStatus) -> None:
    with pytest.raises(InvalidConclusion, match="被拒绝"):
        await ReviewerAgent().run(review_input(), ReviewMemoryIO(status=status), max_steps=16)


async def test_forged_and_main_agent_ids_cannot_replace_independent_observations() -> None:
    value = review_input()

    def forged(request: ChatRequest) -> ChatResponse:
        response = review_response(request)
        data = json.loads(response.message.content or "{}")
        data["checks"][0]["evidence_ids"] = [str(value.conclusion.root_cause.evidence_ids[0])]
        return response.model_copy(
            update={"message": ChatMessage(role="assistant", content=json.dumps(data))}
        )

    io = ReviewMemoryIO(
        llm=FakeLLM(
            [
                ScriptedChatStep(review_response),
                ScriptedChatStep(review_response),
                ScriptedChatStep(forged),
            ]
        )
    )
    with pytest.raises(InvalidConclusion, match="未观察"):
        await ReviewerAgent().run(value, io, max_steps=16)


@pytest.mark.parametrize(
    "message,finish",
    [
        (ChatMessage(role="assistant", content="{}"), "stop"),
        (ChatMessage(role="assistant", content="{}"), "length"),
        (ChatMessage(role="assistant", refusal="拒绝"), "stop"),
    ],
)
async def test_invalid_report_refusal_and_truncation(message: ChatMessage, finish: str) -> None:
    llm = FakeLLM(
        [
            ScriptedChatStep(
                lambda request: ChatResponse(
                    id="invalid",
                    model="fake",
                    message=message,
                    finish_reason=finish,
                )
            )
        ]
    )
    with pytest.raises(ValueError):
        await ReviewerAgent().run(review_input(), ReviewMemoryIO(llm=llm), max_steps=16)


async def test_missing_duplicate_coverage_or_empty_evidence_is_rejected() -> None:
    checks = [
        ReviewCheck(
            alternative=kind,
            outcome="not_supported",
            statement="样本未见异常",
            evidence_ids=(uuid4(),),
        )
        for kind in AlternativeCause
    ]
    for data in (checks[:3], [checks[0]] * 4):
        with pytest.raises(ValidationError):
            ReviewReport(checks=tuple(data))
    with pytest.raises(ValidationError):
        ReviewCheck(
            alternative=AlternativeCause.NETWORK,
            outcome="not_supported",
            statement="无样本",
            evidence_ids=(),
        )


@pytest.mark.parametrize(
    "confidence,mode,expected", [(1.0, "clear", 1.0), (0.0, "contradicted", 0.0)]
)
async def test_confidence_clamps_to_probability_range(
    confidence: float,
    mode: Literal["clear", "contradicted"],
    expected: float,
) -> None:
    value = review_input()
    report, _, _ = await ReviewerAgent().run(value, ReviewMemoryIO(mode), max_steps=16)
    assert (
        adjusted_conclusion(
            value.conclusion.model_copy(update={"confidence": confidence}), report
        ).confidence
        == expected
    )


@pytest.mark.parametrize("budget", [0, 101, True])
async def test_invalid_reviewer_config_rejected(budget: object) -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({"reviewer_max_steps": budget})


@pytest.mark.parametrize(
    "parameters,accepted",
    [
        (
            {
                "service_name": "payment-service",
                "start": SPEC.start.isoformat(),
                "end": SPEC.end.isoformat(),
            },
            True,
        ),
        (
            {
                "service_name": "checkout-service",
                "start": SPEC.start.isoformat(),
                "end": SPEC.end.isoformat(),
            },
            False,
        ),
        (
            {
                "service_name": "payment-service",
                "start": "2026-09-30T00:00:00Z",
                "end": SPEC.end.isoformat(),
            },
            False,
        ),
        (
            {
                "service_name": "payment-service",
                "start": "2026-10-01T01:00:00",
                "end": SPEC.end.isoformat(),
            },
            False,
        ),
    ],
)
async def test_review_scope_cannot_escape_service_and_utc_window(
    parameters: dict[str, str], accepted: bool
) -> None:
    from app.agent.models import FunctionCall

    call = ToolCall(
        id="scope", function=FunctionCall(name="query_traces", arguments=json.dumps(parameters))
    )
    assert within_review_scope(call, SPEC) is accepted


async def test_inconclusive_never_increases_confidence() -> None:
    value = review_input()
    checks = tuple(
        ReviewCheck(
            alternative=kind,
            outcome="inconclusive",
            statement="缺少足够样本",
            evidence_ids=(uuid4(),),
        )
        for kind in AlternativeCause
    )
    report = ReviewReport(checks=checks)
    assert report.verdict == "inconclusive"
    assert adjusted_conclusion(value.conclusion, report).confidence == 0.7


async def test_event_microsecond_end_uses_in_scope_integer_lookback() -> None:
    value = review_input().model_copy(
        update={"spec": SPEC.model_copy(update={"end": SPEC.end + timedelta(microseconds=1)})}
    )
    io = ReviewMemoryIO()
    report, _, _ = await ReviewerAgent().run(value, io, max_steps=16)
    changes = io.calls[1]
    assert report.verdict == "clear"
    assert changes.function.parsed_arguments["lookback_seconds"] == 3600
    assert within_review_scope(changes, value.spec)
