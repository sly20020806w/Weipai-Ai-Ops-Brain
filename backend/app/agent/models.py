"""网关与 Fake 共用的文本 chat、函数调用及 embeddings 契约。"""

import json
import math
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

NonEmptyText = Annotated[str, Field(min_length=1)]
JsonObject = dict[str, JsonValue]
_arguments_adapter = TypeAdapter(JsonObject)


def _reject_constant(value: str) -> None:
    raise ValueError("Tool 参数必须使用标准 JSON 数值")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Tool 参数必须使用有限数值")
    return number


class LLMModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)


class FunctionCall(LLMModel):
    name: NonEmptyText
    arguments: str

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: str) -> str:
        _arguments_adapter.validate_python(
            json.loads(value, parse_constant=_reject_constant, parse_float=_finite_float)
        )
        return value

    @property
    def parsed_arguments(self) -> JsonObject:
        return _arguments_adapter.validate_json(self.arguments)


class ToolCall(LLMModel):
    id: NonEmptyText
    type: Literal["function"] = "function"
    function: FunctionCall


class ToolFunction(LLMModel):
    name: NonEmptyText
    description: str = ""
    parameters: JsonObject


class ToolDefinition(LLMModel):
    type: Literal["function"] = "function"
    function: ToolFunction


class ChatMessage(LLMModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: NonEmptyText | None = None
    refusal: str | None = None

    @model_validator(mode="after")
    def validate_role_fields(self) -> "ChatMessage":
        if self.role != "assistant" and (self.tool_calls or self.refusal is not None):
            raise ValueError("仅 assistant 消息可包含 tool_calls/refusal")
        if self.role == "tool":
            if self.tool_call_id is None or self.content is None:
                raise ValueError("tool 消息必须有 tool_call_id 和 content")
        elif self.tool_call_id is not None:
            raise ValueError("仅 tool 消息可包含 tool_call_id")
        if self.content is None and not self.tool_calls and self.refusal is None:
            raise ValueError("消息必须包含 content、tool_calls 或 refusal")
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("tool_calls 的 ID 不可重复")
        return self

    def to_wire(self) -> JsonObject:
        message: JsonObject = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.model_dump(mode="json") for call in self.tool_calls]
        if self.tool_call_id is not None:
            message["tool_call_id"] = self.tool_call_id
        if self.refusal is not None:
            message["refusal"] = self.refusal
        return message


class ChatRequest(LLMModel):
    messages: Annotated[tuple[ChatMessage, ...], Field(min_length=1)]
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: Literal["auto", "none", "required"] = "auto"

    @model_validator(mode="after")
    def validate_tools(self) -> "ChatRequest":
        if self.tool_choice == "required" and not self.tools:
            raise ValueError("tool_choice=required 必须提供 tools")
        if len({tool.function.name for tool in self.tools}) != len(self.tools):
            raise ValueError("Tool 名称不可重复")
        return self


class TokenUsage(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int = Field(ge=0)


class ChatResponse(LLMModel):
    id: NonEmptyText
    model: NonEmptyText
    message: ChatMessage
    finish_reason: NonEmptyText
    usage: TokenUsage | None = None

    @model_validator(mode="after")
    def validate_assistant(self) -> "ChatResponse":
        if self.message.role != "assistant":
            raise ValueError("chat 响应必须是 assistant 消息")
        return self


class EmbeddingRequest(LLMModel):
    inputs: Annotated[tuple[NonEmptyText, ...], Field(min_length=1)]


class EmbeddingResponse(LLMModel):
    model: NonEmptyText
    vectors: Annotated[
        tuple[Annotated[tuple[float, ...], Field(min_length=1)], ...], Field(min_length=1)
    ]
    usage: TokenUsage | None = None

    @model_validator(mode="after")
    def validate_vectors(self) -> "EmbeddingResponse":
        if len({len(vector) for vector in self.vectors}) != 1 or any(
            not math.isfinite(value) for vector in self.vectors for value in vector
        ):
            raise ValueError("embedding 向量必须等长且只包含有限数值")
        return self
