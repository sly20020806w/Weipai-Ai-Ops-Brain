"""Fake LLM 严格按脚本返回调查与向量结果，不构建 HTTP 客户端。"""

import httpx2 as httpx
import pytest

from app.agent.client import GatewayTimeout, LLMClient
from app.agent.fake import ChatStep, EmbeddingStep, FakeLLM, FakeScriptError, create_llm_client
from app.agent.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    FunctionCall,
    ToolCall,
)
from app.config import Settings

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


@pytest.fixture(autouse=True)
def forbid_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Fake LLM 禁止构建 HTTP 客户端")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", blocked)


@pytest.mark.asyncio
async def test_scripted_tool_call_observation_answer_and_embeddings() -> None:
    question = ChatRequest(messages=(ChatMessage(role="user", content="调查支付服务"),))
    tool_call = ToolCall(
        id="call-context",
        function=FunctionCall(
            name="get_service_context", arguments='{"service":"payment-service"}'
        ),
    )
    first = ChatResponse(
        id="fake-chat-1",
        model="fake-chat",
        message=ChatMessage(role="assistant", tool_calls=(tool_call,)),
        finish_reason="tool_calls",
    )
    follow_up = ChatRequest(
        messages=(
            *question.messages,
            first.message,
            ChatMessage(role="tool", tool_call_id="call-context", content='{"evidence_id":"E1"}'),
        )
    )
    answer = ChatResponse(
        id="fake-chat-2",
        model="fake-chat",
        message=ChatMessage(role="assistant", content="根据 E1，继续检查最近变更"),
        finish_reason="stop",
    )
    embedding = EmbeddingRequest(inputs=("支付规则",))
    vectors = EmbeddingResponse(model="fake-embedding", vectors=((0.1, 0.2),))
    client: LLMClient = create_llm_client(
        Settings(APP_ENV="test"),
        fake_steps=(
            ChatStep(question, first),
            ChatStep(follow_up, answer),
            EmbeddingStep(embedding, vectors),
        ),
    )
    assert isinstance(client, FakeLLM)
    assert await client.chat(question) == first
    assert await client.chat(follow_up) == answer
    assert await client.embeddings(embedding) == vectors
    assert client.calls == (question, follow_up, embedding)
    assert client.remaining_steps == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_fake_mismatch_exhaustion_and_close_fail_explicitly() -> None:
    request = ChatRequest(messages=(ChatMessage(role="user", content="问题 A"),))
    response = ChatResponse(
        id="fake-1",
        model="fake-chat",
        message=ChatMessage(role="assistant", content="回答 A"),
        finish_reason="stop",
    )
    client = FakeLLM((ChatStep(request, response),))
    with pytest.raises(FakeScriptError, match="不匹配"):
        await client.chat(ChatRequest(messages=(ChatMessage(role="user", content="问题 B"),)))
    assert client.remaining_steps == 1
    assert await client.chat(request) == response
    with pytest.raises(FakeScriptError, match="耗尽"):
        await client.chat(request)
    await client.aclose()
    with pytest.raises(FakeScriptError, match="关闭"):
        await client.chat(request)


@pytest.mark.asyncio
async def test_fake_scripts_errors_without_retrying() -> None:
    request = EmbeddingRequest(inputs=("支付规则",))
    client = FakeLLM((EmbeddingStep(request, GatewayTimeout("脚本模拟超时")),))
    with pytest.raises(GatewayTimeout, match="模拟超时"):
        await client.embeddings(request)
    assert client.calls == (request,)
    assert client.remaining_steps == 0


@pytest.mark.asyncio
async def test_fake_rejects_wrong_embedding_count() -> None:
    request = EmbeddingRequest(inputs=("支付规则", "发布规范"))
    client = FakeLLM(
        (EmbeddingStep(request, EmbeddingResponse(model="fake-embedding", vectors=((0.1,),))),)
    )
    with pytest.raises(FakeScriptError, match="数量"):
        await client.embeddings(request)


def test_fake_is_default_and_rejects_http_transport() -> None:
    settings = Settings(APP_ENV="local")
    assert settings.llm_mode == "fake"
    assert isinstance(create_llm_client(settings), FakeLLM)
    with pytest.raises(ValueError, match="transport"):
        create_llm_client(
            settings, transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )
