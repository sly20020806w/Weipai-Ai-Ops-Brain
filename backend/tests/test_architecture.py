"""架构评审离线契约，禁止实际 HTTP/DNS/socket。"""

import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.tasks.architecture.engine import assess, validate_citations
from app.tasks.architecture.models import DIMENSIONS, ReviewDraft, ReviewSubmission
from app.tasks.architecture.scenario import SAMPLE_PROPOSAL, SAMPLE_STANDARD, sample_response
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import WorkflowInput
from app.tools.models import JsonObject
from app.tools.registry import json_object

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def payload(proposal: str = SAMPLE_PROPOSAL) -> JsonObject:
    ids = {
        role: str(uuid4()) for role in ("proposal", "runbooks", "context", "standards", "incidents")
    }
    return json_object(
        {
            "sources": ids,
            "snapshots": {
                ids["proposal"]: {"proposal": proposal},
                ids["runbooks"]: {"matches": []},
                ids["context"]: {"service_name": "payment-service", "nodes": [], "edges": []},
                ids["standards"]: {"matches": [{"entry": {"content": SAMPLE_STANDARD}}]},
                ids["incidents"]: {"matches": []},
            },
        }
    )


def draft(value: JsonObject | None = None) -> ReviewDraft:
    response = sample_response(
        ChatRequest(messages=(ChatMessage(role="user", content=json.dumps(value or payload())),))
    )
    return ReviewDraft.model_validate_json(response.message.content or "{}")


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_every_required_dimension_cannot_be_omitted(dimension: str) -> None:
    data = draft().model_dump(mode="json")
    data["dimensions"] = [d for d in data["dimensions"] if d["dimension"] != dimension]
    with pytest.raises(ValidationError):
        ReviewDraft.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("change", ["duplicate", "reorder", "extra"])
def test_duplicate_reordered_and_extra_dimensions_rejected(change: str) -> None:
    data = draft().model_dump(mode="json")
    if change == "duplicate":
        data["dimensions"][1] = data["dimensions"][0]
    elif change == "reorder":
        data["dimensions"].reverse()
    else:
        data["dimensions"].append(data["dimensions"][0])
    with pytest.raises(ValidationError):
        ReviewDraft.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "field,value",
    [
        ("proposal", " "),
        ("proposal", "x" * 20001),
        ("service_name", "bad service"),
        ("request_id", "invalid"),
        ("title", ""),
        ("execute", True),
    ],
)
def test_invalid_input_and_extra_execution_parameters_rejected(field: str, value: object) -> None:
    data = {
        "request_id": str(uuid4()),
        "service_name": "payment-service",
        "title": "支付设计",
        "proposal": "设计",
    }
    data[field] = value  # type: ignore[assignment]
    with pytest.raises(ValidationError):
        ReviewSubmission.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("change", ["missing", "invented", "quote", "duplicate"])
def test_missing_forged_and_false_citations_rejected(change: str) -> None:
    value = payload()
    data = draft(value).model_dump(mode="json")
    citations = data["dimensions"][0]["citations"]
    if change == "missing":
        data["dimensions"][0]["citations"] = []
    elif change == "invented":
        citations[0]["evidence_id"] = str(uuid4())
    elif change == "quote":
        citations[0]["quote"] = "数据库已通过恢复演练"
    else:
        citations.append(citations[0])
    with pytest.raises((ValidationError, ValueError)):
        report = ReviewDraft.model_validate_json(json.dumps(data))
        snapshots = json.loads(json.dumps(value))["snapshots"]
        validate_citations(report, {UUID(k): v for k, v in snapshots.items()})


@pytest.mark.asyncio
async def test_fake_sample_all_dimensions_risk_standards_and_unknowns() -> None:
    value = payload()
    llm = FakeLLM([ScriptedChatStep(sample_response)])
    report = await assess(llm, value)
    assert tuple(d.dimension for d in report.dimensions) == DIMENSIONS
    assert [d.outcome for d in report.dimensions] == ["risk", "risk"] + ["unknown"] * 10
    snapshots = json.loads(json.dumps(value))["snapshots"]
    validate_citations(report, {UUID(k): v for k, v in snapshots.items()})
    assert len(llm.calls) == 1
    request = llm.calls[0]
    assert isinstance(request, ChatRequest) and request.tool_choice == "none" and not request.tools


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["truncated", "refusal", "tool", "invalid_json"])
async def test_incomplete_or_tool_call_model_response_rejected(bad: str) -> None:
    value = payload()

    def response(request: ChatRequest) -> ChatResponse:
        original = sample_response(request)
        if bad == "truncated":
            return original.model_copy(update={"finish_reason": "length"})
        if bad == "refusal":
            return original.model_copy(
                update={"message": ChatMessage(role="assistant", refusal="拒绝")}
            )
        if bad == "tool":
            return original.model_copy(
                update={
                    "message": ChatMessage(
                        role="assistant",
                        tool_calls=(
                            ToolCall(
                                id="write",
                                function=FunctionCall(name="execute_action", arguments="{}"),
                            ),
                        ),
                    )
                }
            )
        return original.model_copy(
            update={"message": ChatMessage(role="assistant", content="bad json")}
        )

    with pytest.raises(ValueError):
        await assess(FakeLLM([ScriptedChatStep(response)]), value)


def test_fake_does_not_invent_risks_for_unscripted_proposals() -> None:
    report = draft(payload("方案计划消除单点数据库，详细材料尚未提供"))
    assert all(d.outcome == "unknown" for d in report.dimensions)


@pytest.mark.parametrize(
    "change",
    [
        {"inspection_mode": "inspection"},
        {"ticket_id": "T1"},
        {"release_id": "R1"},
        {"execution_enabled": True},
        {"architecture_review": "yes"},
    ],
)
def test_architecture_workflow_cannot_mix_execution_or_other_scenarios(
    change: dict[str, object],
) -> None:
    options = WorkflowInput(str(uuid4()), architecture_review=True)
    with pytest.raises(ValueError):
        validate_workflow_input(replace(options, **change))  # type: ignore[arg-type]
