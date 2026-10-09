"""Step 8：权限判定矩阵、配置边界与未声明风险；禁止真实网络。"""

import json
import socket
from collections.abc import Iterator
from itertools import permutations

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.api.main import create_app
from app.config import Settings
from app.policy.engine import PolicyEngine, create_policy_engine
from app.policy.models import (
    PolicyAction,
    PolicyConfig,
    PolicyDecision,
    PolicyEnvironment,
    PolicyRule,
    RiskLevel,
)


@pytest.fixture(autouse=True)
def forbid_policy_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[str] = []

    def blocked(*args: object, **kwargs: object) -> None:
        attempts.append("network")
        raise AssertionError("Step 8 测试禁止实际网络连接")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    yield
    assert attempts == [], "Policy 测试曾尝试访问网络"


@pytest.mark.parametrize("environment", list(PolicyEnvironment))
@pytest.mark.parametrize("risk", list(RiskLevel))
def test_default_matrix(environment: PolicyEnvironment, risk: RiskLevel) -> None:
    result = PolicyEngine(environment).evaluate(PolicyAction(name="sample_action", risk_level=risk))
    assert result.decision is (
        PolicyDecision.ALLOW if risk is RiskLevel.L0 else PolicyDecision.NEED_APPROVAL
    )
    assert result.risk_level is risk
    assert result.environment is environment
    assert result.action_name == "sample_action"
    assert result.matched_rule_ids == ()
    assert result.reason


CONFIGURED_RULES = PolicyConfig(
    rules=(
        PolicyRule(
            id="deny-destructive",
            risk_levels=(RiskLevel.L5,),
            decision=PolicyDecision.DENY,
            reason="破坏性动作禁止",
        ),
        PolicyRule(
            id="test-deny",
            risk_levels=(RiskLevel.L0, RiskLevel.L3),
            environments=(PolicyEnvironment.TEST,),
            decision=PolicyDecision.DENY,
            reason="测试环境限制",
        ),
        PolicyRule(
            id="test-low-risk",
            risk_levels=(RiskLevel.L1,),
            environments=(PolicyEnvironment.TEST,),
            decision=PolicyDecision.ALLOW,
            reason="明确允许的测试动作",
        ),
        PolicyRule(
            id="production-read-review",
            risk_levels=(RiskLevel.L0,),
            environments=(PolicyEnvironment.PRODUCTION,),
            decision=PolicyDecision.NEED_APPROVAL,
            reason="生产读取需审批",
        ),
        PolicyRule(
            id="production-deny",
            risk_levels=(RiskLevel.L2, RiskLevel.L4),
            environments=(PolicyEnvironment.PRODUCTION,),
            decision=PolicyDecision.DENY,
            reason="生产变更限制",
        ),
    )
)

# 独立列出期望矩阵；未命中显式规则的格子仍使用安全缺省值。
CONFIGURED_MATRIX = (
    (PolicyEnvironment.TEST, RiskLevel.L0, PolicyDecision.DENY),
    (PolicyEnvironment.TEST, RiskLevel.L1, PolicyDecision.ALLOW),
    (PolicyEnvironment.TEST, RiskLevel.L2, PolicyDecision.NEED_APPROVAL),
    (PolicyEnvironment.TEST, RiskLevel.L3, PolicyDecision.DENY),
    (PolicyEnvironment.TEST, RiskLevel.L4, PolicyDecision.NEED_APPROVAL),
    (PolicyEnvironment.TEST, RiskLevel.L5, PolicyDecision.DENY),
    (PolicyEnvironment.PRODUCTION, RiskLevel.L0, PolicyDecision.NEED_APPROVAL),
    (PolicyEnvironment.PRODUCTION, RiskLevel.L1, PolicyDecision.NEED_APPROVAL),
    (PolicyEnvironment.PRODUCTION, RiskLevel.L2, PolicyDecision.DENY),
    (PolicyEnvironment.PRODUCTION, RiskLevel.L3, PolicyDecision.NEED_APPROVAL),
    (PolicyEnvironment.PRODUCTION, RiskLevel.L4, PolicyDecision.DENY),
    (PolicyEnvironment.PRODUCTION, RiskLevel.L5, PolicyDecision.DENY),
)


@pytest.mark.parametrize("environment,risk,expected", CONFIGURED_MATRIX)
def test_configured_matrix(
    environment: PolicyEnvironment, risk: RiskLevel, expected: PolicyDecision
) -> None:
    result = PolicyEngine(environment, CONFIGURED_RULES).evaluate(
        PolicyAction(name="sample_action", risk_level=risk)
    )
    assert result.decision is expected


@pytest.mark.parametrize("environment", list(PolicyEnvironment))
@pytest.mark.parametrize("config", [PolicyConfig(), CONFIGURED_RULES])
def test_undeclared_and_null_risk_are_identical_to_l5(
    environment: PolicyEnvironment, config: PolicyConfig
) -> None:
    engine = PolicyEngine(environment, config)
    declared = engine.evaluate(PolicyAction(name="sample_action", risk_level=RiskLevel.L5))
    missing = engine.evaluate(PolicyAction(name="sample_action"))
    explicit_null = engine.evaluate(
        PolicyAction.model_validate({"name": "sample_action", "risk_level": None})
    )
    assert missing == explicit_null == declared
    assert missing.model_dump(mode="json") == declared.model_dump(mode="json")


@pytest.mark.parametrize("order", list(permutations(list(PolicyDecision))))
def test_deny_wins_independent_of_order(order: tuple[PolicyDecision, ...]) -> None:
    config = PolicyConfig(
        rules=tuple(
            PolicyRule(
                id=f"rule-{decision.value}",
                risk_levels=(RiskLevel.L3,),
                decision=decision,
                reason=decision.value,
            )
            for decision in order
        )
    )
    result = PolicyEngine(PolicyEnvironment.PRODUCTION, config).evaluate(
        PolicyAction(name="rollback_prod", risk_level=RiskLevel.L3)
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reason == "deny"
    assert result.matched_rule_ids == ("rule-allow", "rule-deny", "rule-need_approval")


@pytest.mark.parametrize("reverse", [False, True])
def test_approval_wins_over_allow_and_reasons_are_deterministic(reverse: bool) -> None:
    rules = (
        PolicyRule(
            id="z-review",
            risk_levels=(RiskLevel.L1,),
            decision=PolicyDecision.NEED_APPROVAL,
            reason="原因乙",
        ),
        PolicyRule(
            id="a-review",
            risk_levels=(RiskLevel.L1,),
            decision=PolicyDecision.NEED_APPROVAL,
            reason="原因甲",
        ),
        PolicyRule(
            id="allow", risk_levels=(RiskLevel.L1,), decision=PolicyDecision.ALLOW, reason="可允许"
        ),
    )
    result = PolicyEngine(
        PolicyEnvironment.TEST, PolicyConfig(rules=rules[::-1] if reverse else rules)
    ).evaluate(PolicyAction(name="sample_action", risk_level=RiskLevel.L1))
    assert result.decision is PolicyDecision.NEED_APPROVAL
    assert result.reason == "原因甲；原因乙"
    assert result.matched_rule_ids == ("a-review", "allow", "z-review")


@pytest.mark.parametrize(
    "name,environment,risk,expected",
    [
        ("inspect_test", PolicyEnvironment.TEST, RiskLevel.L1, PolicyDecision.ALLOW),
        ("inspect_test_extra", PolicyEnvironment.TEST, RiskLevel.L1, PolicyDecision.NEED_APPROVAL),
        ("inspect_test", PolicyEnvironment.PRODUCTION, RiskLevel.L1, PolicyDecision.NEED_APPROVAL),
        ("inspect_test", PolicyEnvironment.TEST, RiskLevel.L2, PolicyDecision.NEED_APPROVAL),
        ("inspect_test", PolicyEnvironment.TEST, RiskLevel.L5, PolicyDecision.NEED_APPROVAL),
    ],
)
def test_rule_requires_exact_name_environment_and_risk_match(
    name: str, environment: PolicyEnvironment, risk: RiskLevel, expected: PolicyDecision
) -> None:
    config = PolicyConfig(
        rules=(
            PolicyRule(
                id="test-only",
                risk_levels=(RiskLevel.L1,),
                environments=(PolicyEnvironment.TEST,),
                action_names=("inspect_test",),
                decision=PolicyDecision.ALLOW,
                reason="只允许指定测试动作",
            ),
        )
    )
    assert (
        PolicyEngine(environment, config)
        .evaluate(PolicyAction(name=name, risk_level=risk))
        .decision
        is expected
    )


@pytest.mark.parametrize("environment", list(PolicyEnvironment))
def test_environment_factory_and_default_settings(
    environment: PolicyEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", environment.value)
    engine = create_policy_engine(Settings())
    assert engine.environment is environment
    assert engine.config == PolicyConfig()
    assert (
        engine.evaluate(PolicyAction(name="execute_action")).decision
        is PolicyDecision.NEED_APPROVAL
    )


def test_json_environment_rules_and_serializable_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("POLICY_CONFIG", CONFIGURED_RULES.model_dump_json())
    settings = Settings()
    engine = create_policy_engine(settings)
    result = engine.evaluate(PolicyAction(name="execute_prod_sql", risk_level=RiskLevel.L4))
    assert settings.policy_config == CONFIGURED_RULES
    assert result.model_dump(mode="json") == {
        "action_name": "execute_prod_sql",
        "risk_level": "L4",
        "environment": "production",
        "decision": "deny",
        "reason": "生产变更限制",
        "matched_rule_ids": ["production-deny"],
    }
    assert engine.config.rules[0].environments == tuple(PolicyEnvironment)


VALID_RULE: dict[str, object] = {
    "id": "restrict-sql",
    "risk_levels": ["L4"],
    "environments": ["production"],
    "action_names": ["execute_prod_sql"],
    "decision": "deny",
    "reason": "生产 SQL 禁止",
}


@pytest.mark.parametrize(
    "override",
    [
        {"risk_levels": []},
        {"risk_levels": ["L6"]},
        {"risk_levels": [0]},
        {"risk_levels": ["L0", "L0"]},
        {"environments": []},
        {"environments": ["prod"]},
        {"environments": ["test", "test"]},
        {"action_names": [" "]},
        {"action_names": ["execute_prod_sql", "execute_prod_sql"]},
        {"decision": "approved"},
        {"decision": None},
        {"reason": " \n "},
        {"reason": 123},
        {"id": ""},
        {"id": 123},
        {"risk_level": "L0"},
    ],
)
def test_invalid_rule_environment_fails_startup(
    override: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("POLICY_CONFIG", json.dumps({"rules": [VALID_RULE | override]}))
    with pytest.raises(ValidationError):
        create_app()


@pytest.mark.parametrize("missing", ["id", "risk_levels", "decision", "reason"])
def test_incomplete_rule_is_rejected(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = dict(VALID_RULE)
    del rule[missing]
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("POLICY_CONFIG", json.dumps({"rules": [rule]}))
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "config", [{"rule": []}, {"rules": None}, {"rules": [VALID_RULE, VALID_RULE]}, []]
)
def test_invalid_config_does_not_fall_back_to_allow(
    config: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("POLICY_CONFIG", json.dumps(config))
    with pytest.raises(ValidationError):
        Settings()


def test_malformed_json_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("POLICY_CONFIG", "{invalid")
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.parametrize("risk", ["L6", "l0", "", 0, False, [], {}])
def test_invalid_action_risk_is_rejected(risk: object) -> None:
    with pytest.raises(ValidationError):
        PolicyAction.model_validate({"name": "sample_action", "risk_level": risk})


@pytest.mark.parametrize("name", ["", " ", " rollback_prod", "rollback_prod ", "rollback*", 123])
def test_invalid_action_name_is_rejected(name: object) -> None:
    with pytest.raises(ValidationError):
        PolicyAction.model_validate({"name": name})


def test_agent_cannot_supply_approval_or_environment_in_action() -> None:
    for field in ("environment", "approved", "decision"):
        with pytest.raises(ValidationError):
            PolicyAction.model_validate({"name": "rollback_prod", field: "allow"})


def test_configuration_action_and_result_are_immutable() -> None:
    engine = PolicyEngine(PolicyEnvironment.TEST, CONFIGURED_RULES)
    action = PolicyAction(name="delete_database")
    result = engine.evaluate(action)
    for instance, attribute, value in (
        (action, "risk_level", RiskLevel.L0),
        (engine.config, "rules", ()),
        (engine.config.rules[0], "decision", PolicyDecision.ALLOW),
        (result, "decision", PolicyDecision.ALLOW),
    ):
        with pytest.raises(ValidationError, match="frozen"):
            setattr(instance, attribute, value)
    assert engine.evaluate(action) == result


def test_runtime_inputs_require_validated_types() -> None:
    with pytest.raises(TypeError, match="PolicyEnvironment"):
        PolicyEngine("production")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PolicyConfig"):
        PolicyEngine(PolicyEnvironment.TEST, {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PolicyAction"):
        PolicyEngine(PolicyEnvironment.TEST).evaluate({"name": "sample_action"})  # type: ignore[arg-type]
