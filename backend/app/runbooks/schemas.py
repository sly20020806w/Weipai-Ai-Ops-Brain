"""SPEC 中的完整 Runbook 内容；方案字段不授予动作执行权限。"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, StringConstraints, model_validator

from app.policy.models import RiskLevel
from app.tools.models import JsonObject, ToolModel

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class RunbookMaturity(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    VERIFIED = "verified"
    SEMI_AUTOMATED = "semi_automated"
    APPROVAL_AUTOMATED = "approval_automated"
    SELF_HEALING = "self_healing"


class AutomationLevel(StrEnum):
    MANUAL = "manual"
    SEMI_AUTOMATED = "semi_automated"
    APPROVAL_AUTOMATED = "approval_automated"
    SELF_HEALING = "self_healing"


class RunbookCondition(ToolModel):
    field: Literal["service_name", "title", "task_source"]
    operator: Literal["equals", "contains"]
    value: Text


class DiagnosticStep(ToolModel):
    description: Text
    tool_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")]
    parameters: JsonObject
    risk_level: Literal[RiskLevel.L0]


class HandlingStep(ToolModel):
    description: Text
    risk_level: RiskLevel


class RunbookContent(ToolModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,127}$")]
    description: Text
    source: Text
    applicability_conditions: Annotated[
        tuple[RunbookCondition, ...], Field(min_length=1, max_length=30)
    ]
    exclusion_conditions: Annotated[tuple[RunbookCondition, ...], Field(max_length=30)]
    diagnostic_steps: Annotated[tuple[DiagnosticStep, ...], Field(min_length=1, max_length=30)]
    handling_steps: Annotated[tuple[HandlingStep, ...], Field(min_length=1, max_length=30)]
    risk_level: RiskLevel
    rollback_plan: Text
    verification_steps: Annotated[tuple[Text, ...], Field(min_length=1, max_length=30)]

    @model_validator(mode="after")
    def risk_covers_steps(self) -> "RunbookContent":
        if any(step.risk_level > self.risk_level for step in self.handling_steps):
            raise ValueError("Runbook 风险等级不能低于处理步骤")
        # 防止 JSON 参数带 NaN 或非 JSON 对象；不能借参数更改 Tool 声明。
        from app.tools.registry import json_object

        for step in self.diagnostic_steps:
            json_object(step.parameters)
        return self


class RunbookDraft(RunbookContent):
    success_count: Annotated[int, Field(ge=0)]
    failure_count: Annotated[int, Field(ge=0)]
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    automation_level: AutomationLevel
    maturity: RunbookMaturity


class RunbookView(RunbookDraft):
    id: UUID
    created_at: datetime
    updated_at: datetime
    embedding_model: str
    embedding_dimensions: int
    content_version: int = Field(default=1, ge=1)


class RunbookSearch(ToolModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
    limit: Annotated[int, Field(ge=1, le=100)] = 10


class RunbookHit(ToolModel):
    runbook: RunbookView
    similarity: Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]


class MatchingFacts(ToolModel):
    service_name: Text
    title: Text
    task_source: Text


def condition_holds(condition: RunbookCondition, facts: MatchingFacts) -> bool:
    value: JsonValue = facts.model_dump()[condition.field]
    return (
        value == condition.value
        if condition.operator == "equals"
        else condition.value in str(value)
    )


def applicability(runbook: RunbookView, facts: MatchingFacts) -> tuple[bool, str]:
    runbook, facts = RunbookView.model_validate(runbook), MatchingFacts.model_validate(facts)
    if any(condition_holds(condition, facts) for condition in runbook.exclusion_conditions):
        return False, "排除条件命中，退出 Runbook 并转自主调查"
    if not all(condition_holds(condition, facts) for condition in runbook.applicability_conditions):
        return False, "适用条件未全部满足，转自主调查"
    if runbook.maturity is RunbookMaturity.DRAFT:
        return False, "Runbook 尚未经验证，转自主调查"
    if runbook.maturity is RunbookMaturity.REVIEWED:
        return True, "已人工审核，仅按 L0 诊断步骤试用，写动作仍需审批与独立验证"
    return True, "适用条件全部满足且未命中排除条件，按 Runbook 诊断步骤调查"
