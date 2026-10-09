"""专家纯循环、真实引用、模型协议与 Holmes HTTP mock 边界。"""

import json
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.agent.experts.engine import ExpertAgent
from app.agent.experts.models import (
    TOOL_ALLOWLISTS,
    ExpertAdvice,
    ExpertKind,
    ExpertOpinion,
    ExpertRequest,
)
from app.agent.experts.scenario import expert_response
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import AgentStepLimit, EvidenceClaim, InvalidConclusion
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.config import Settings
from app.connectors.holmes.client import HolmesError, HTTPHolmesConnector
from app.connectors.holmes.factory import create_holmes_connector
from app.connectors.holmes.fake import FakeHolmesConnector
from app.connectors.holmes.models import HolmesConfig, HolmesRequest
from app.connectors.models import ReaderCredentials
from app.tools.models import DispatchStatus
from tests.test_main_agent import SPEC, MemoryIO

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("forbid_llm_network")]


def expert_request(kind: ExpertKind = ExpertKind.DATABASE) -> ExpertRequest:
    return ExpertRequest(**SPEC.model_dump(), expert=kind, question="请核查依赖与容量")


class ExpertMemoryIO(MemoryIO):
    async def think(self, request: ChatRequest) -> ChatResponse:  # type: ignore[override]
        return await self.llm.chat(request)

    async def call(self, call: ToolCall):  # type: ignore[no-untyped-def, override]
        return await super().call(1, call)


@pytest.mark.parametrize("kind", [kind for kind in ExpertKind if kind is not ExpertKind.HOLMESGPT])
async def test_six_roles_produce_referenced_opinions(kind: ExpertKind) -> None:
    io = ExpertMemoryIO(
        FakeLLM(
            [
                ScriptedChatStep(lambda value: expert_response(kind, value)),
                ScriptedChatStep(lambda value: expert_response(kind, value)),
            ]
        )
    )
    advice = await ExpertAgent().run(expert_request(kind), io, budget=8)
    assert advice.steps == 3 and advice.expert is kind
    assert advice.opinion.evidence_ids == {UUID(value) for value in io.ids}
    assert set(io.tool_names) <= TOOL_ALLOWLISTS[kind]
    assert "consult_expert" not in TOOL_ALLOWLISTS[kind]


@pytest.mark.parametrize("budget", [0, 1, 2])
async def test_expert_budget_rejects_before_extra_work(budget: int) -> None:
    io = ExpertMemoryIO(
        FakeLLM(
            [
                ScriptedChatStep(lambda value: expert_response(ExpertKind.DATABASE, value)),
                ScriptedChatStep(lambda value: expert_response(ExpertKind.DATABASE, value)),
            ]
        )
    )
    with pytest.raises(AgentStepLimit):
        await ExpertAgent().run(expert_request(), io, budget=budget)
    assert len(io.tool_names) == (1 if budget == 2 else 0)


async def test_forged_opinion_id_rejected() -> None:
    opinion = ExpertOpinion(
        assessment=EvidenceClaim(statement="伪造", evidence_ids=(uuid4(),)), confidence=0.9
    )
    response = ChatResponse(
        id="forged",
        model="fake",
        finish_reason="stop",
        message=ChatMessage(role="assistant", content=opinion.model_dump_json()),
    )
    io = ExpertMemoryIO(FakeLLM([ScriptedChatStep(lambda value: response)]))
    with pytest.raises(InvalidConclusion, match="未观察"):
        await ExpertAgent().run(expert_request(), io, budget=8)


@pytest.mark.parametrize("finish", ["length", "tool_calls"])
async def test_incomplete_opinion_rejected(finish: str) -> None:
    io = ExpertMemoryIO(
        FakeLLM(
            [
                ScriptedChatStep(
                    lambda value: ChatResponse(
                        id="incomplete",
                        model="fake",
                        finish_reason=finish,
                        message=ChatMessage(role="assistant", content="{}"),
                    )
                )
            ]
        )
    )
    with pytest.raises(InvalidConclusion):
        await ExpertAgent().run(expert_request(), io, budget=8)


async def test_denied_tool_never_becomes_opinion_evidence() -> None:
    io = ExpertMemoryIO(
        FakeLLM(
            [
                ScriptedChatStep(lambda value: expert_response(ExpertKind.DATABASE, value)),
                ScriptedChatStep(lambda value: expert_response(ExpertKind.DATABASE, value)),
            ]
        ),
        DispatchStatus.REJECTED,
    )
    with pytest.raises(InvalidConclusion):
        await ExpertAgent().run(expert_request(), io, budget=8)
    assert io.ids == []


async def test_duplicate_expert_call_id_rejected() -> None:
    response = ChatResponse(
        id="repeat",
        model="fake",
        finish_reason="tool_calls",
        message=ChatMessage(
            role="assistant",
            tool_calls=(
                ToolCall(id="same", function=FunctionCall(name="query_logs", arguments="{}")),
            ),
        ),
    )
    io = ExpertMemoryIO(
        FakeLLM(
            [ScriptedChatStep(lambda value: response), ScriptedChatStep(lambda value: response)]
        )
    )
    with pytest.raises(InvalidConclusion, match="重复"):
        await ExpertAgent().run(expert_request(), io, budget=8)
    assert len(io.tool_names) == 1


def holmes_request() -> HolmesRequest:
    return HolmesRequest(
        service_name="payment-service",
        question="RCA 线索？",
        context={"evidence": [{"id": str(uuid4()), "result": {"connections": 480}}]},
        output_schema=ExpertOpinion.model_json_schema(),
    )


async def test_fake_holmes_opinion_preserves_actual_ids_and_closes() -> None:
    request = holmes_request()
    async with create_holmes_connector(Settings(APP_ENV="test")) as connector:
        assert isinstance(connector, FakeHolmesConnector)
        response = await connector.analyze(request)
        opinion = ExpertOpinion.model_validate_json(response.analysis)
        evidence = request.context["evidence"]
        assert isinstance(evidence, list) and isinstance(evidence[0], dict)
        assert opinion.evidence_ids == {UUID(str(evidence[0]["id"]))}
        assert connector.calls == [request]
    with pytest.raises(HolmesError, match="关闭"):
        await connector.analyze(request)


async def test_holmes_native_chat_protocol_uses_config_and_snapshot_only() -> None:
    request = holmes_request()
    calls = []

    def handler(value: httpx.Request) -> httpx.Response:
        calls.append(value)
        assert str(value.url) == "https://holmes.example.test/api/chat"
        assert value.headers["authorization"] == "Bearer mock-reader"
        body = json.loads(value.content)
        assert body["model"] == "company-gateway-model"
        assert body["stream"] is False and body["enable_tool_approval"] is True
        assert body["response_format"]["json_schema"]["schema"] == request.output_schema
        assert json.loads(body["ask"]) == request.model_dump(mode="json")
        return httpx.Response(200, json={"analysis": "{}", "tool_calls": []})

    async with HTTPHolmesConnector(
        HolmesConfig(
            base_url="https://holmes.example.test",
            model="company-gateway-model",
            snapshot_only=True,
        ),
        ReaderCredentials(connector="holmes", token=SecretStr("mock-reader")),
        transport=httpx.MockTransport(handler),
    ) as connector:
        assert (await connector.analyze(request)).analysis == "{}"
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["tools", "approval", "invalid", "redirect", "timeout"])
async def test_holmes_rejects_bypass_and_sanitizes_errors(mode: str) -> None:
    def handler(value: httpx.Request) -> httpx.Response:
        if mode == "timeout":
            raise httpx.ReadTimeout("sensitive-token", request=value)
        return httpx.Response(
            302 if mode == "redirect" else 200,
            json={"analysis": "ok", "tool_calls": ["kubectl"]}
            if mode == "tools"
            else {"analysis": "ok", "approval_required": True}
            if mode == "approval"
            else {"analysis": None},
            headers={"Location": "https://other.example.test"},
        )

    async with HTTPHolmesConnector(
        HolmesConfig(
            base_url="https://holmes.example.test", model="configured", snapshot_only=True
        ),
        ReaderCredentials(connector="holmes", token=SecretStr("sensitive-token")),
        transport=httpx.MockTransport(handler),
    ) as connector:
        with pytest.raises(HolmesError) as caught:
            await connector.analyze(holmes_request())
        assert "sensitive-token" not in str(caught.value)


@pytest.mark.parametrize(
    "url", ["https://name:secret@holmes.test", "file:///tmp/x", "https://x/?q=1"]
)
async def test_invalid_holmes_config_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        HolmesConfig(base_url=url, model="model", snapshot_only=True)


async def test_local_mode_cannot_enable_real_connectors() -> None:
    with pytest.raises(ValidationError):
        Settings(APP_ENV="test", CONNECTOR_MODE="real")
    with pytest.raises(ValidationError):
        HolmesConfig(base_url="https://holmes.test", model="configured", snapshot_only=False)


async def test_unisolated_real_holmes_fails_before_any_network() -> None:
    settings = Settings(
        APP_ENV="staging",
        CONNECTOR_MODE="real",
        HOLMES_CONFIG={
            "base_url": "https://holmes.test",
            "model": "company-model",
            "snapshot_only": True,
        },
        CONNECTOR_READER_TOKENS={"holmes": "mock-reader"},
    )
    with pytest.raises(HolmesError, match="隔离"):
        create_holmes_connector(settings)


async def test_advice_cannot_cite_unobserved_ids() -> None:
    with pytest.raises(ValidationError):
        ExpertAdvice(
            expert=ExpertKind.DATABASE,
            steps=1,
            observed_ids=(uuid4(),),
            opinion=ExpertOpinion(
                assessment=EvidenceClaim(statement="无效", evidence_ids=(uuid4(),)), confidence=0.5
            ),
        )
