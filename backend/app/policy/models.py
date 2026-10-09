"""Policy 的风险声明、环境变量规则与判定结果；不执行动作。"""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")]
Reason = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class RiskLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"


class PolicyEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    NEED_APPROVAL = "need_approval"
    DENY = "deny"


class PolicyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class RunbookPolicyContext(PolicyModel):
    runbook_id: UUID
    revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    maturity: Literal[
        "draft", "reviewed", "verified", "semi_automated", "approval_automated", "self_healing"
    ]
    trusted: bool = False
    review_evidence_id: UUID | None = None
    state_evidence_id: UUID | None = None


class PolicyAction(PolicyModel):
    """只描述动作名称和风险；未声明（包括 null）统一规范为 L5。"""

    name: Identifier
    risk_level: RiskLevel = RiskLevel.L5
    runbook: RunbookPolicyContext | None = None

    @field_validator("risk_level", mode="before")
    @classmethod
    def normalize_missing_risk(cls, value: object) -> object:
        return RiskLevel.L5 if value is None else value


class PolicyRule(PolicyModel):
    id: Identifier
    risk_levels: Annotated[tuple[RiskLevel, ...], Field(min_length=1)]
    decision: PolicyDecision
    reason: Reason
    environments: Annotated[tuple[PolicyEnvironment, ...], Field(min_length=1)] = tuple(
        PolicyEnvironment
    )
    # 空列表匹配全部动作；非空列表只按名称精确匹配，不支持通配符。
    action_names: tuple[Identifier, ...] = ()

    @field_validator("risk_levels", "environments", "action_names")
    @classmethod
    def reject_duplicate_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("同一规则的匹配条件不可重复")
        return value


class PolicyConfig(PolicyModel):
    rules: tuple[PolicyRule, ...] = ()

    @field_validator("rules")
    @classmethod
    def reject_duplicate_rule_ids(cls, value: tuple[PolicyRule, ...]) -> tuple[PolicyRule, ...]:
        if len({rule.id for rule in value}) != len(value):
            raise ValueError("Policy 规则 ID 不可重复")
        return value


class PolicyResult(PolicyModel):
    action_name: Identifier
    risk_level: RiskLevel
    environment: PolicyEnvironment
    decision: PolicyDecision
    reason: Reason
    matched_rule_ids: tuple[Identifier, ...] = ()
