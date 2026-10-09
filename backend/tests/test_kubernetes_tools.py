"""Step 12 三个 Tool 的证据、审计、Policy、Replay 验收。"""

from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.kubernetes.fake import FakeKubernetesConnector
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyConfig, PolicyDecision, PolicyEnvironment, PolicyRule, RiskLevel
from app.tools.dispatcher import ToolDispatcher
from app.tools.kubernetes import register_kubernetes_tools
from app.tools.models import DispatchMode, DispatchStatus, JsonObject
from app.tools.registry import DuplicateTool, ToolRegistry
from tests.test_tools import MemoryLedger

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
PARAMS: JsonObject = {"namespace": "payment", "service_name": "payment-service"}
TOOLS = ["get_k8s_status", "get_service_runtime", "query_events"]


@pytest_asyncio.fixture
async def ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MemoryLedger]:
    async with AsyncSession() as session, session.begin():
        monkeypatch.setattr(session, "get", AsyncMock(return_value=object()))
        yield MemoryLedger(session)


def registry_for(connector: FakeKubernetesConnector) -> ToolRegistry:
    registry = ToolRegistry()
    register_kubernetes_tools(registry, connector)
    return registry


def test_three_l0_schemas_and_duplicate_registration() -> None:
    connector = FakeKubernetesConnector()
    registry = registry_for(connector)
    assert {tool.name for tool in registry.declarations()} == set(TOOLS)
    for tool in registry.declarations():
        assert tool.risk_level is RiskLevel.L0
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["additionalProperties"] is False
    with pytest.raises(DuplicateTool):
        register_kubernetes_tools(registry, connector)


@pytest.mark.parametrize("tool_name", TOOLS)
@pytest.mark.asyncio
async def test_each_call_has_exactly_one_evidence_and_audit(
    ledger: MemoryLedger, tool_name: str
) -> None:
    async with FakeKubernetesConnector() as connector:
        result = await ToolDispatcher(
            registry_for(connector), PolicyEngine(PolicyEnvironment.TEST), ledger
        ).dispatch(task_id=uuid4(), tool_name=tool_name, parameters=PARAMS, actor="fake-agent")
    assert result.status is DispatchStatus.SUCCEEDED and result.result is not None
    assert result.policy.risk_level is RiskLevel.L0
    assert len(ledger.evidence) == len(ledger.audits) == 1
    evidence, audit = ledger.evidence[0], ledger.audits[0]
    assert result.evidence_id == evidence.id == audit.evidence_id
    assert evidence.source_tool == tool_name and evidence.parameters == PARAMS
    assert evidence.result_snapshot == result.result and audit.operation == tool_name
    assert evidence.collected_at.utcoffset() == timedelta(0)
    assert result.result["cluster_name"] == "ack-fake"
    field, count = {
        "get_k8s_status": ("deployments", 1),
        "get_service_runtime": ("pods", 3),
        "query_events": ("events", 1),
    }[tool_name]
    records = result.result[field]
    assert isinstance(records, list) and len(records) == count
    assert isinstance(records[0], dict)
    assert records[0]["apiVersion"] == ("apps/v1" if field == "deployments" else "v1")
    assert "api_version" not in records[0]
    if field == "deployments":
        status = records[0]["status"]
        assert isinstance(status, dict)
        assert status["readyReplicas"] == 2 and "ready_replicas" not in status
    assert (
        records[0]["kind"] == {"deployments": "Deployment", "pods": "Pod", "events": "Event"}[field]
    )


@pytest.mark.parametrize("tool_name", TOOLS)
@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.NEED_APPROVAL])
@pytest.mark.asyncio
async def test_policy_blocks_before_connector(
    ledger: MemoryLedger, tool_name: str, decision: PolicyDecision
) -> None:
    connector = FakeKubernetesConnector()
    await connector.aclose()
    policy = PolicyEngine(
        PolicyEnvironment.TEST,
        PolicyConfig(
            rules=(
                PolicyRule(
                    id="k8s-gate",
                    action_names=(tool_name,),
                    risk_levels=(RiskLevel.L0,),
                    decision=decision,
                    reason="专项门禁测试",
                ),
            )
        ),
    )
    result = await ToolDispatcher(registry_for(connector), policy, ledger).dispatch(
        task_id=uuid4(), tool_name=tool_name, parameters=PARAMS, actor="fake-agent"
    )
    assert result.status is DispatchStatus.REJECTED
    assert result.error_code == (
        "policy_denied" if decision is PolicyDecision.DENY else "approval_required"
    )
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize("tool_name", TOOLS)
@pytest.mark.asyncio
async def test_replay_closed_connector_uses_original_evidence(
    ledger: MemoryLedger, tool_name: str
) -> None:
    connector = FakeKubernetesConnector()
    dispatcher = ToolDispatcher(
        registry_for(connector), PolicyEngine(PolicyEnvironment.TEST), ledger
    )
    task_id = uuid4()
    live = await dispatcher.dispatch(
        task_id=task_id, tool_name=tool_name, parameters=PARAMS, actor="fake-agent"
    )
    assert live.status is DispatchStatus.SUCCEEDED
    await connector.aclose()
    replay = await dispatcher.dispatch(
        task_id=task_id,
        tool_name=tool_name,
        parameters=PARAMS,
        actor="fake-agent",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=ledger.evidence[0].collected_at + timedelta(seconds=1),
    )
    assert replay.status is DispatchStatus.REPLAYED
    assert replay.evidence_id == live.evidence_id and replay.result == live.result
    assert len(ledger.evidence) == 1 and len(ledger.audits) == 2


@pytest.mark.parametrize("tool_name", TOOLS)
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"namespace": "../payment"},
        {"namespace": "payment", "service_name": "payment-service,other"},
        {**PARAMS, "token": "cannot-pass-identity"},
        {**PARAMS, "base_url": "https://other.invalid"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_inputs_never_read(
    ledger: MemoryLedger, tool_name: str, params: JsonObject
) -> None:
    connector = FakeKubernetesConnector()
    await connector.aclose()
    result = await ToolDispatcher(
        registry_for(connector), PolicyEngine(PolicyEnvironment.TEST), ledger
    ).dispatch(task_id=uuid4(), tool_name=tool_name, parameters=params, actor="fake-agent")
    assert result.status is DispatchStatus.REJECTED and result.error_code == "invalid_parameters"
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize("tool_name", TOOLS)
@pytest.mark.asyncio
async def test_closed_connector_failure_has_no_success_evidence(
    ledger: MemoryLedger, tool_name: str
) -> None:
    connector = FakeKubernetesConnector()
    await connector.aclose()
    result = await ToolDispatcher(
        registry_for(connector), PolicyEngine(PolicyEnvironment.TEST), ledger
    ).dispatch(task_id=uuid4(), tool_name=tool_name, parameters=PARAMS, actor="fake-agent")
    assert result.status is DispatchStatus.FAILED and result.error_code == "tool_failed"
    assert ledger.evidence == [] and len(ledger.audits) == 1
