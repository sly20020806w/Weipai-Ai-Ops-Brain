"""高级 Tool 的声明和调用结果；风险复用 Policy 的唯一枚举。"""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from app.policy.models import PolicyResult, RiskLevel

JsonObject = dict[str, JsonValue]
ToolName = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=64)
]


class ToolModel(BaseModel):
    """入出参统一使用严格对象 schema，拒绝未声明字段。"""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
        validate_default=True,
    )


class ToolDeclaration(ToolModel):
    name: ToolName
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    risk_level: RiskLevel = RiskLevel.L5
    input_schema: JsonObject
    output_schema: JsonObject


class DispatchMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class DispatchStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REPLAYED = "replayed"
    REJECTED = "rejected"
    FAILED = "failed"


class DispatchResult(ToolModel):
    status: DispatchStatus
    policy: PolicyResult
    audit_id: UUID
    evidence_id: UUID | None = None
    result: JsonObject | None = None
    error_code: str | None = Field(default=None, min_length=1)
