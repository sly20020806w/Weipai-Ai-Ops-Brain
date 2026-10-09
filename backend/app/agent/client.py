"""公司 AI 网关的异步 OpenAI 兼容客户端；只生成内容，不执行 Tool。"""

import asyncio
from types import TracebackType
from typing import Literal, Protocol, Self

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    JsonObject,
    TokenUsage,
    ToolCall,
)
from app.config import Settings


class GatewayError(RuntimeError):
    """错误不包含凭证、请求内容或网关返回的原始错误正文。"""


class GatewayTimeout(GatewayError):
    pass


class GatewayTransportError(GatewayError):
    pass


class GatewayResponseError(GatewayError):
    pass


class GatewayHTTPError(GatewayError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"AI 网关返回 HTTP {status_code}")


class LLMClient(Protocol):
    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    async def embeddings(self, request: EmbeddingRequest) -> EmbeddingResponse: ...

    async def aclose(self) -> None: ...


class _Envelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _AssistantMessage(_Envelope):
    role: Literal["assistant"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
    refusal: str | None = None


class _Choice(_Envelope):
    index: Literal[0]
    message: _AssistantMessage
    finish_reason: str = Field(min_length=1)


class _ChatEnvelope(_Envelope):
    id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    choices: tuple[_Choice, ...] = Field(min_length=1, max_length=1)
    usage: TokenUsage | None = None


class _EmbeddingItem(_Envelope):
    index: int = Field(ge=0)
    embedding: tuple[float, ...] = Field(min_length=1)


class _EmbeddingEnvelope(_Envelope):
    model: str = Field(min_length=1)
    data: tuple[_EmbeddingItem, ...] = Field(min_length=1)
    usage: TokenUsage | None = None


class GatewayClient:
    def __init__(self, settings: Settings, *, transport: httpx.MockTransport | None = None) -> None:
        if settings.llm_mode != "gateway":
            raise ValueError("GatewayClient 需要 LLM_MODE=gateway")
        if settings.app_env in {"local", "test"} and not isinstance(transport, httpx.MockTransport):
            raise ValueError("local/test 环境只允许 Fake LLM 或 HTTP MockTransport")
        self._config = settings.require_gateway_config()
        self._http = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={"Authorization": f"Bearer {self._config.api_key.get_secret_value()}"},
            timeout=self._config.timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, payload: JsonObject) -> bytes:
        for attempt in range(self._config.max_retries + 1):
            try:
                response = await self._http.post(path, json=payload)
            except httpx.TimeoutException:
                if attempt == self._config.max_retries:
                    raise GatewayTimeout("AI 网关请求超时，已达到重试上限") from None
                await asyncio.sleep(min(30, self._config.retry_delay_seconds * 2**attempt))
                continue
            except httpx.RequestError:
                raise GatewayTransportError("AI 网关连接失败") from None
            if not response.is_success:
                raise GatewayHTTPError(response.status_code)
            return response.content
        raise AssertionError("不可到达的重试状态")

    async def chat(self, request: ChatRequest) -> ChatResponse:
        payload: JsonObject = {
            "model": self._config.chat_model,
            "messages": [message.to_wire() for message in request.messages],
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [tool.model_dump(mode="json") for tool in request.tools]
            payload["tool_choice"] = request.tool_choice
        raw = await self._post("chat/completions", payload)
        try:
            result = _ChatEnvelope.model_validate_json(raw)
            choice = result.choices[0]
            return ChatResponse(
                id=result.id,
                model=result.model,
                message=ChatMessage(
                    role=choice.message.role,
                    content=choice.message.content,
                    tool_calls=choice.message.tool_calls or (),
                    refusal=choice.message.refusal,
                ),
                finish_reason=choice.finish_reason,
                usage=result.usage,
            )
        except (ValidationError, ValueError):
            raise GatewayResponseError("AI 网关 chat 响应不符合协议") from None

    async def embeddings(self, request: EmbeddingRequest) -> EmbeddingResponse:
        raw = await self._post(
            "embeddings",
            {
                "model": self._config.embedding_model,
                "input": list(request.inputs),
                "encoding_format": "float",
            },
        )
        try:
            result = _EmbeddingEnvelope.model_validate_json(raw)
            ordered = sorted(result.data, key=lambda item: item.index)
            if [item.index for item in ordered] != list(range(len(request.inputs))):
                raise ValueError("embedding 数量或下标不匹配")
            return EmbeddingResponse(
                model=result.model,
                vectors=tuple(item.embedding for item in ordered),
                usage=result.usage,
            )
        except (ValidationError, ValueError):
            raise GatewayResponseError("AI 网关 embeddings 响应不符合协议") from None
