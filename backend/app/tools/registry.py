"""只声明和注册 Tool；实现函数仅由 Dispatcher 取得并调用。"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import cast

from pydantic import BaseModel, JsonValue

from app.policy.models import PolicyAction, RiskLevel
from app.tools.models import JsonObject, ToolDeclaration, ToolModel


class DuplicateTool(ValueError):
    pass


class ToolNotFound(LookupError):
    pass


def json_object(value: object) -> JsonObject:
    """拒绝非 JSON 数据并深复制，避免参数或结果在留痕前被修改。"""

    def validate(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("Tool JSON 对象的键必须为字符串")
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ValueError("Tool 参数和结果只能使用标准 JSON 数据")

    if not isinstance(value, dict):
        raise ValueError("Tool 参数和结果必须为 JSON 对象")
    validate(value)
    return cast(JsonObject, json.loads(json.dumps(value, allow_nan=False)))


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    declaration: ToolDeclaration
    input_model: type[ToolModel]
    output_model: type[ToolModel]
    invoke: Callable[[BaseModel], Awaitable[BaseModel]]

    def prepare(self, parameters: JsonObject) -> tuple[BaseModel, JsonObject]:
        model = self.input_model.model_validate_json(
            json.dumps(json_object(parameters)), strict=True
        )
        return model, json_object(model.model_dump(mode="json"))

    def validate_result(self, value: object) -> JsonObject:
        model = self.output_model.model_validate(value, strict=True)
        return json_object(model.model_dump(mode="json"))

    def validate_snapshot(self, value: object) -> JsonObject:
        snapshot = json_object(value)
        self.output_model.model_validate_json(json.dumps(snapshot), strict=True)
        # schema 校验不能给历史结果补新默认字段；必须原样返回当时快照。
        return snapshot


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}

    def register[Input: ToolModel, Output: ToolModel](
        self,
        *,
        name: str,
        description: str,
        input_model: type[Input],
        output_model: type[Output],
        handler: Callable[[Input], Awaitable[Output]],
        risk_level: RiskLevel | None = None,
    ) -> ToolDeclaration:
        if name in self._tools:
            raise DuplicateTool(f"Tool 名称已注册：{name}")
        if not issubclass(input_model, ToolModel) or not issubclass(output_model, ToolModel):
            raise TypeError("Tool 入出参模型必须继承 ToolModel")
        for model in (input_model, output_model):
            if (
                model.model_config.get("extra") != "forbid"
                or model.model_config.get("strict") is not True
                or model.model_config.get("revalidate_instances") != "always"
            ):
                raise TypeError("Tool 模型不能放宽严格校验和未声明字段限制")
        # callable 只判断可调用；这里还要确认 callable 对象的实现为 async。
        async_call = getattr(handler, "__call__", None)  # noqa: B004
        if not callable(handler) or not (
            iscoroutinefunction(handler) or iscoroutinefunction(async_call)
        ):
            raise TypeError("Tool 实现必须为异步函数")
        declaration = ToolDeclaration(
            name=name,
            description=description,
            risk_level=PolicyAction.model_validate(
                {"name": name, "risk_level": risk_level}
            ).risk_level,
            input_schema=cast(dict[str, JsonValue], input_model.model_json_schema()),
            output_schema=cast(dict[str, JsonValue], output_model.model_json_schema()),
        )

        async def invoke(model: BaseModel) -> BaseModel:
            if not isinstance(model, input_model):
                raise TypeError("Tool 入参类型不匹配")
            return await handler(model)

        self._tools[name] = _RegisteredTool(declaration, input_model, output_model, invoke)
        return declaration.model_copy(deep=True)

    def _get(self, name: str) -> _RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolNotFound(f"Tool 未注册：{name}") from error

    def declarations(self) -> tuple[ToolDeclaration, ...]:
        return tuple(
            self._tools[name].declaration.model_copy(deep=True) for name in sorted(self._tools)
        )
