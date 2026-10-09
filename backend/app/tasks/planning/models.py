"""规划草稿与宿主判定结果；均不包含执行权限或凭证。"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.agent.investigation import EvidenceClaim, Text
from app.policy.models import (
    PolicyDecision,
    PolicyEnvironment,
    PolicyResult,
    RiskLevel,
    RunbookPolicyContext,
)
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import JsonObject, ToolModel, ToolName

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


class RollbackPlan(ToolModel):
    description: Text
    parameters: Annotated[JsonObject, Field(min_length=1)]
    trigger: Text


class VerificationPlan(ToolModel):
    checks: Annotated[tuple[Text, ...], Field(min_length=1, max_length=20)]
    success_criteria: Text
    failure_response: Text


class RollbackParameters(ToolModel):
    from_version: Identifier
    to_version: Identifier

    @model_validator(mode="after")
    def different_versions(self) -> "RollbackParameters":
        if self.from_version == self.to_version:
            raise ValueError("回滚的起始与目标版本不能相同")
        return self


class PlannedAction(ToolModel):
    id: Identifier
    name: ToolName
    service_name: Identifier
    parameters: Annotated[JsonObject, Field(min_length=1)]
    risk_level: RiskLevel = RiskLevel.L5
    rationale: EvidenceClaim
    preconditions: Annotated[tuple[Text, ...], Field(min_length=1, max_length=20)]
    rollback: RollbackPlan
    verification: VerificationPlan

    @field_validator("risk_level", mode="before")
    @classmethod
    def undeclared_risk(cls, value: object) -> object:
        return RiskLevel.L5 if value is None else value

    @model_validator(mode="after")
    def rollback_parameters(self) -> "PlannedAction":
        if self.name == "rollback_prod":
            RollbackParameters.model_validate(self.parameters)
        return self


class ActionPlanDraft(ToolModel):
    summary: EvidenceClaim
    actions: Annotated[tuple[PlannedAction, ...], Field(min_length=1, max_length=10)]

    @model_validator(mode="after")
    def unique_actions(self) -> "ActionPlanDraft":
        if len({action.id for action in self.actions}) != len(self.actions):
            raise ValueError("计划中的动作 ID 不可重复")
        return self


class EvaluatedAction(ToolModel):
    action: PlannedAction
    policy: PolicyResult

    @model_validator(mode="after")
    def aligned(self) -> "EvaluatedAction":
        if (self.action.name, self.action.risk_level) != (
            self.policy.action_name,
            self.policy.risk_level,
        ):
            raise ValueError("Policy 判定必须对应同一动作和宿主风险等级")
        return self


class ActionPlan(ToolModel):
    task_id: UUID
    planning_version: int = Field(ge=1)
    conclusion_evidence_id: UUID
    review_evidence_id: UUID
    environment: PolicyEnvironment
    summary: EvidenceClaim
    actions: Annotated[tuple[EvaluatedAction, ...], Field(min_length=1, max_length=10)]
    runbook: RunbookPolicyContext | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @property
    def decision(self) -> PolicyDecision:
        for decision in (PolicyDecision.DENY, PolicyDecision.NEED_APPROVAL):
            if any(item.policy.decision is decision for item in self.actions):
                return decision
        return PolicyDecision.ALLOW


@dataclass(frozen=True)
class PlanningRequest:
    task: TaskSnapshot
    spec_json: str
    conclusion_evidence_id: str
    review_evidence_id: str


@dataclass(frozen=True)
class PlanningResult:
    evidence_id: str
    plan_json: str
