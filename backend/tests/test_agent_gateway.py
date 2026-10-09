"""Step 7：只经 HTTP mock 验证公司网关协议；任何实际网络连接均失败。"""

import asyncio
import json
from unittest.mock import AsyncMock, call

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.agent.client import (
    GatewayClient,
    GatewayHTTPError,
    GatewayResponseError,
    GatewayTimeout,
    GatewayTransportError,
)
from app.agent.fake import create_llm_client
from app.agent.models import (
    ChatMessage,
    ChatRequest,
    EmbeddingRequest,
    FunctionCall,
    JsonObject,
    ToolDefinition,
    ToolFunction,
)
from app.config import Settings

pytestmark = pytest.mark.usefixtures("forbid_llm_network")

GATEWAY_ENV = {
    "APP_ENV": "test",
    "LLM_MODE": "gateway",
    "AI_GATEWAY_BASE_URL": "https://gateway.example.invalid/company/v1",
    "AI_GATEWAY_API_KEY": "fake-gateway-key",
    "AI_GATEWAY_CHAT_MODEL": "company-chat-test",
    "AI_GATEWAY_EMBEDDING_MODEL": "company-embedding-test",
    "AI_GATEWAY_TIMEOUT_SECONDS": "1.25",
    "AI_GATEWAY_MAX_RETRIES": "2",
    "AI_GATEWAY_RETRY_DELAY_SECONDS": "0",
}
QUESTION = ChatRequest(messages=(ChatMessage(role="user", content="检查 payment-service"),))
TOOLS = (
    ToolDefinition(
        function=ToolFunction(
            name="get_service_context",
            description="获取服务上下文",
            parameters={"type": "object", "properties": {"service": {"type": "string"}}},
        )
    ),
)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in GATEWAY_ENV.items():
        monkeypatch.setenv(name, value)
    return Settings()


def chat_body(message: JsonObject | None = None) -> JsonObject:
    return {
        "id": "chat-mock-1",
        "object": "chat.completion",
        "model": "company-chat-test",
        "created": 1791230400,
        "choices": [
            {
                "index": 0,
                "message": message
                if message is not None
                else {
                    "role": "assistant",
                    "content": "服务状态正常",
                    "tool_calls": None,
                    "refusal": None,
                    "annotations": [],
                },
                "finish_reason": "tool_calls" if message is not None else "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_chat_uses_configured_gateway_model_auth_and_timeout(settings: Settings) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://gateway.example.invalid/company/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-gateway-key"
        assert request.headers["content-type"] == "application/json"
        assert request.extensions["timeout"] == dict(connect=1.25, read=1.25, write=1.25, pool=1.25)
        assert json.loads(request.content) == {
            "model": "company-chat-test",
            "messages": [{"role": "user", "content": "检查 payment-service"}],
            "stream": False,
        }
        return httpx.Response(200, json=chat_body())

    async with GatewayClient(settings, transport=httpx.MockTransport(handle)) as client:
        response = await client.chat(QUESTION)
    assert len(requests) == 1
    assert response.message.content == "服务状态正常"
    assert response.message.tool_calls == ()
    assert response.finish_reason == "stop"
    assert response.usage is not None and response.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_multiple_tool_calls_and_tool_results_round_trip(settings: Settings) -> None:
    tool_request = ChatRequest(messages=QUESTION.messages, tools=TOOLS, tool_choice="required")
    requests: list[JsonObject] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert payload["tools"] == [tool.model_dump(mode="json") for tool in TOOLS]
            assert payload["tool_choice"] == "required"
            return httpx.Response(
                200,
                json=chat_body(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": identifier,
                                "type": "function",
                                "function": {
                                    "name": "get_service_context",
                                    "arguments": '{"service":"payment-service"}',
                                },
                            }
                            for identifier in ("call-1", "call-2")
                        ],
                    }
                ),
            )
        assert [message["tool_call_id"] for message in payload["messages"][-2:]] == [
            "call-1",
            "call-2",
        ]
        return httpx.Response(200, json=chat_body())

    async with GatewayClient(settings, transport=httpx.MockTransport(handle)) as client:
        first = await client.chat(tool_request)
        assert [call.id for call in first.message.tool_calls] == ["call-1", "call-2"]
        assert first.message.tool_calls[0].function.parsed_arguments == {
            "service": "payment-service"
        }
        assert first.message.tool_calls[0].function.name == "get_service_context"
        follow_up = ChatRequest(
            messages=(
                *QUESTION.messages,
                first.message,
                *(
                    ChatMessage(role="tool", tool_call_id=call.id, content='{"evidence_id":"E1"}')
                    for call in first.message.tool_calls
                ),
            ),
            tools=TOOLS,
        )
        assert (await client.chat(follow_up)).message.content == "服务状态正常"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_embeddings_use_config_and_restore_input_order(settings: Settings) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://gateway.example.invalid/company/v1/embeddings"
        assert request.headers["authorization"] == "Bearer fake-gateway-key"
        assert json.loads(request.content) == {
            "model": "company-embedding-test",
            "input": ["支付规则", "发布规范"],
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={
                "model": "company-embedding-test",
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    async with GatewayClient(settings, transport=httpx.MockTransport(handle)) as client:
        response = await client.embeddings(EmbeddingRequest(inputs=("支付规则", "发布规范")))
    assert response.vectors == ((0.1, 0.2), (0.3, 0.4))
    assert response.usage is not None and response.usage.total_tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["chat", "embeddings"])
async def test_timeout_retries_then_succeeds(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setenv("AI_GATEWAY_RETRY_DELAY_SECONDS", "0.25")
    requests: list[httpx.Request] = []
    sleep = AsyncMock()
    monkeypatch.setattr("app.agent.client.asyncio.sleep", sleep)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) <= 2:
            raise httpx.ReadTimeout("模拟超时", request=request)
        body = (
            chat_body()
            if operation == "chat"
            else {"model": "company-embedding-test", "data": [{"index": 0, "embedding": [0.1]}]}
        )
        return httpx.Response(200, json=body)

    async with GatewayClient(Settings(), transport=httpx.MockTransport(handle)) as client:
        if operation == "chat":
            await client.chat(QUESTION)
        else:
            await client.embeddings(EmbeddingRequest(inputs=("支付规则",)))
    assert len(requests) == 3
    assert all(request.content == requests[0].content for request in requests)
    assert sleep.await_count == 2
    assert sleep.await_args_list == [call(0.25), call(0.5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 2])
async def test_timeout_exhaustion_is_bounded_and_redacted(
    settings: Settings, retries: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_GATEWAY_MAX_RETRIES", str(retries))
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("fake-gateway-key 请求内容", request=request)

    async with GatewayClient(Settings(), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(GatewayTimeout, match="重试上限") as error:
            await client.chat(QUESTION)
    assert attempts == retries + 1
    assert "fake-gateway-key" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 400, 401, 429, 500])
async def test_http_errors_are_not_retried_or_redirected(settings: Settings, status: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://other.example.invalid/v1/chat/completions"},
            json={"error": "fake-gateway-key 请求内容"},
        )

    async with GatewayClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(GatewayHTTPError) as error:
            await client.chat(QUESTION)
    assert error.value.status_code == status
    assert len(requests) == 1
    assert "fake-gateway-key" not in str(error.value)


@pytest.mark.asyncio
async def test_transport_failure_and_cancellation_propagate_safely(settings: Settings) -> None:
    def failed(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fake-gateway-key", request=request)

    async with GatewayClient(settings, transport=httpx.MockTransport(failed)) as client:
        with pytest.raises(GatewayTransportError, match="连接失败"):
            await client.chat(QUESTION)

    def cancelled(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with GatewayClient(settings, transport=httpx.MockTransport(cancelled)) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.chat(QUESTION)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments", ["{", "[]", "null", '{"x":NaN}', '{"x":Infinity}', '{"x":1e309}']
)
async def test_malformed_tool_arguments_are_rejected(settings: Settings, arguments: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=chat_body(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "get_service_context", "arguments": arguments},
                        }
                    ],
                }
            ),
        )

    async with GatewayClient(settings, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(GatewayResponseError, match="chat"):
            await client.chat(QUESTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not JSON", b"{}", b'{"choices":[]}'])
async def test_malformed_chat_response_is_rejected(settings: Settings, body: bytes) -> None:
    async with GatewayClient(
        settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(GatewayResponseError):
            await client.chat(QUESTION)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "错误角色"},
        {"role": "assistant", "content": None},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-duplicate",
                    "type": "function",
                    "function": {"name": "get_service_context", "arguments": "{}"},
                }
            ]
            * 2,
        },
    ],
)
async def test_invalid_assistant_messages_are_rejected(
    settings: Settings, message: JsonObject
) -> None:
    async with GatewayClient(
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=chat_body(message))),
    ) as client:
        with pytest.raises(GatewayResponseError):
            await client.chat(QUESTION)


@pytest.mark.asyncio
async def test_refusal_and_gateway_factory(settings: Settings) -> None:
    client = create_llm_client(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=chat_body({"role": "assistant", "content": None, "refusal": "无法回答"})
            )
        ),
    )
    assert isinstance(client, GatewayClient)
    try:
        result = await client.chat(QUESTION)
        assert result.message.refusal == "无法回答"
        assert result.message.tool_calls == ()
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e309"])
async def test_nonfinite_embeddings_are_rejected(settings: Settings, value: str) -> None:
    body = (
        '{"model":"company-embedding-test","data":[{"index":0,"embedding":[' + value + "]}]}"
    ).encode()
    async with GatewayClient(
        settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(GatewayResponseError):
            await client.embeddings(EmbeddingRequest(inputs=("支付规则",)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [0.1]}],
        [{"index": 0, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}],
        [{"index": 0, "embedding": [0.1]}, {"index": 2, "embedding": [0.2]}],
        [{"index": 0, "embedding": []}, {"index": 1, "embedding": [0.2]}],
        [{"index": 0, "embedding": [0.1]}, {"index": 1, "embedding": [0.2, 0.3]}],
        [{"index": 0, "embedding": "base64"}, {"index": 1, "embedding": [0.2]}],
        [{"index": 0, "embedding": [True]}, {"index": 1, "embedding": [0.2]}],
    ],
)
async def test_invalid_embeddings_are_rejected(settings: Settings, data: object) -> None:
    async with GatewayClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"model": "company-embedding-test", "data": data}
            )
        ),
    ) as client:
        with pytest.raises(GatewayResponseError, match="embeddings"):
            await client.embeddings(EmbeddingRequest(inputs=("支付规则", "发布规范")))


def test_local_and_test_gateway_clients_require_http_mock(settings: Settings) -> None:
    with pytest.raises(ValueError, match="MockTransport"):
        GatewayClient(settings)
    with pytest.raises(ValueError, match="LLM_MODE"):
        GatewayClient(Settings(APP_ENV="local", LLM_MODE="fake"))


@pytest.mark.parametrize(
    "name,value",
    [
        ("LLM_MODE", "invalid"),
        ("AI_GATEWAY_BASE_URL", "ftp://gateway.example.invalid/v1"),
        ("AI_GATEWAY_BASE_URL", "https://user:password@gateway.example.invalid/v1"),
        ("AI_GATEWAY_BASE_URL", "https://gateway.example.invalid/v1?key=secret"),
        ("AI_GATEWAY_BASE_URL", "https://gateway.example.invalid/v1#fragment"),
        ("AI_GATEWAY_BASE_URL", "https://gateway.example.invalid:invalid/v1"),
        ("AI_GATEWAY_API_KEY", ""),
        ("AI_GATEWAY_API_KEY", "secret\nheader"),
        ("AI_GATEWAY_CHAT_MODEL", " "),
        ("AI_GATEWAY_EMBEDDING_MODEL", ""),
        ("AI_GATEWAY_TIMEOUT_SECONDS", "0"),
        ("AI_GATEWAY_TIMEOUT_SECONDS", "nan"),
        ("AI_GATEWAY_MAX_RETRIES", "-1"),
        ("AI_GATEWAY_MAX_RETRIES", "6"),
        ("AI_GATEWAY_RETRY_DELAY_SECONDS", "-0.1"),
    ],
)
def test_invalid_gateway_environment_fails_validation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "name",
    [
        "AI_GATEWAY_BASE_URL",
        "AI_GATEWAY_API_KEY",
        "AI_GATEWAY_CHAT_MODEL",
        "AI_GATEWAY_EMBEDDING_MODEL",
    ],
)
def test_gateway_required_variables_and_secret_redaction(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(ValidationError, match=name) as error:
        Settings()
    assert "fake-gateway-key" not in str(error.value)
    assert "fake-gateway-key" not in repr(settings)
    assert "fake-gateway-key" not in repr(settings.require_gateway_config())


def test_request_validation_does_not_allow_invalid_tool_messages() -> None:
    with pytest.raises(ValidationError, match="tool_call_id"):
        ChatMessage(role="tool", content="result")
    with pytest.raises(ValidationError, match="required"):
        ChatRequest(messages=QUESTION.messages, tool_choice="required")
    with pytest.raises(ValidationError):
        EmbeddingRequest(inputs=())
    assert FunctionCall(name="test", arguments='{"nested":{"value":1}}').parsed_arguments == {
        "nested": {"value": 1}
    }
