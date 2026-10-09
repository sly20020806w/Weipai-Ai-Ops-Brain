"""可脚本化、完全离线的 LLM；请求不匹配或脚本耗尽时明确失败。"""

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx2 as httpx

from app.agent.client import GatewayClient, GatewayError, LLMClient
from app.agent.models import ChatRequest, ChatResponse, EmbeddingRequest, EmbeddingResponse
from app.config import Settings


class FakeScriptError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatStep:
    request: ChatRequest
    response: ChatResponse | GatewayError


@dataclass(frozen=True)
class EmbeddingStep:
    request: EmbeddingRequest
    response: EmbeddingResponse | GatewayError


@dataclass(frozen=True)
class ScriptedChatStep:
    """动态 Evidence ID 场景：显式有限脚本，响应函数可检查本轮实际请求。"""

    respond: Callable[[ChatRequest], ChatResponse]


type FakeStep = ChatStep | EmbeddingStep | ScriptedChatStep


class FakeLLM:
    def __init__(self, steps: Iterable[FakeStep] = ()) -> None:
        self._steps = deque(steps)
        self._calls: list[ChatRequest | EmbeddingRequest] = []
        self._closed = False

    @property
    def calls(self) -> tuple[ChatRequest | EmbeddingRequest, ...]:
        return tuple(self._calls)

    @property
    def remaining_steps(self) -> int:
        return len(self._steps)

    def _next(self, request: ChatRequest | EmbeddingRequest) -> FakeStep:
        if self._closed:
            raise FakeScriptError("Fake LLM 已关闭")
        if not self._steps:
            raise FakeScriptError("Fake LLM 脚本已耗尽")
        step = self._steps[0]
        if isinstance(step, ScriptedChatStep):
            if not isinstance(request, ChatRequest):
                raise FakeScriptError("Fake LLM 当前动态步骤不是 chat")
        elif step.request != request:
            raise FakeScriptError("Fake LLM 请求与下一步脚本不匹配")
        self._steps.popleft()
        self._calls.append(request.model_copy(deep=True))
        return step

    async def chat(self, request: ChatRequest) -> ChatResponse:
        step = self._next(request)
        if isinstance(step, ScriptedChatStep):
            return step.respond(request.model_copy(deep=True)).model_copy(deep=True)
        if not isinstance(step, ChatStep):
            raise FakeScriptError("Fake LLM 当前步骤不是 chat")
        if isinstance(step.response, GatewayError):
            raise step.response
        return step.response.model_copy(deep=True)

    async def embeddings(self, request: EmbeddingRequest) -> EmbeddingResponse:
        step = self._next(request)
        if not isinstance(step, EmbeddingStep):
            raise FakeScriptError("Fake LLM 当前步骤不是 embeddings")
        if isinstance(step.response, GatewayError):
            raise step.response
        if len(step.response.vectors) != len(request.inputs):
            raise FakeScriptError("Fake embedding 向量数量与输入不匹配")
        return step.response.model_copy(deep=True)

    async def aclose(self) -> None:
        self._closed = True


def create_llm_client(
    settings: Settings,
    *,
    fake_steps: Iterable[FakeStep] = (),
    transport: httpx.MockTransport | None = None,
) -> LLMClient:
    if settings.llm_mode == "fake":
        if transport is not None:
            raise ValueError("Fake LLM 不使用 HTTP transport")
        return FakeLLM(fake_steps)
    return GatewayClient(settings, transport=transport)
