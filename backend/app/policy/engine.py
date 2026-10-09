"""无 IO 的 Policy 判定；配置规则优先，冲突取最严格结果。"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.policy.models import (
    PolicyAction,
    PolicyConfig,
    PolicyDecision,
    PolicyEnvironment,
    PolicyResult,
    RiskLevel,
)

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True, slots=True)
class PolicyEngine:
    environment: PolicyEnvironment
    config: PolicyConfig = field(default_factory=PolicyConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.environment, PolicyEnvironment):
            raise TypeError("Policy 环境必须使用 PolicyEnvironment")
        if not isinstance(self.config, PolicyConfig):
            raise TypeError("Policy 配置必须使用已校验的 PolicyConfig")

    def evaluate(self, action: PolicyAction) -> PolicyResult:
        if not isinstance(action, PolicyAction):
            raise TypeError("Policy 动作必须使用已校验的 PolicyAction")
        matching = sorted(
            (
                rule
                for rule in self.config.rules
                if action.risk_level in rule.risk_levels
                and self.environment in rule.environments
                and (not rule.action_names or action.name in rule.action_names)
            ),
            key=lambda rule: rule.id,
        )
        if matching:
            # 与规则声明顺序无关；deny 永远不能被 allow 或审批覆盖。
            decision = next(
                decision
                for decision in (
                    PolicyDecision.DENY,
                    PolicyDecision.NEED_APPROVAL,
                    PolicyDecision.ALLOW,
                )
                if any(rule.decision is decision for rule in matching)
            )
            reason = "；".join(rule.reason for rule in matching if rule.decision is decision)
        elif action.risk_level is RiskLevel.L0:
            decision = PolicyDecision.ALLOW
            reason = "默认规则：L0 只读动作允许"
        else:
            decision = PolicyDecision.NEED_APPROVAL
            reason = "默认规则：L1–L5 动作需要审批"
        if (
            action.runbook is not None
            and action.risk_level is not RiskLevel.L0
            and decision is PolicyDecision.ALLOW
        ):
            context = action.runbook
            if (
                context.maturity != "self_healing"
                or not context.trusted
                or context.review_evidence_id is None
                or context.state_evidence_id is None
                or action.risk_level in {RiskLevel.L3, RiskLevel.L4, RiskLevel.L5}
            ):
                decision = PolicyDecision.NEED_APPROVAL
                reason = (
                    "Runbook 尚未达到经人工审核和独立验证的 Self-Healing，"
                    "或动作属于高风险；需要审批"
                )
        return PolicyResult(
            action_name=action.name,
            risk_level=action.risk_level,
            environment=self.environment,
            decision=decision,
            reason=reason,
            matched_rule_ids=tuple(rule.id for rule in matching),
        )


def create_policy_engine(settings: "Settings") -> PolicyEngine:
    """环境来自进程 Settings，不接受 Agent 提供的目标环境。"""
    return PolicyEngine(PolicyEnvironment(settings.app_env), settings.policy_config)
