"""两个 Tool 经唯一 Dispatcher 的 L0、Evidence、Policy 与 Replay 验收。"""

from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.changes.fake import (
    FakeArgoCDConnector,
    FakeCIConnector,
    FakeConfigCenterConnector,
    FakeGitConnector,
)
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyConfig, PolicyDecision, PolicyEnvironment, PolicyRule, RiskLevel
from app.tools.changes import register_change_tools
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus, JsonObject
from app.tools.registry import DuplicateTool, ToolRegistry
from tests.test_tools import MemoryLedger

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
PARAMS: dict[str, JsonObject] = {
    "compare_versions": {
        "service_name": "payment-service",
        "from_version": "v2.3.6",
        "to_version": "v2.3.7",
    },
    "get_recent_deployments": {
        "service_name": "payment-service",
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-01T02:00:00Z",
    },
}


@pytest_asyncio.fixture
async def ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MemoryLedger]:
    async with AsyncSession() as session, session.begin():
        monkeypatch.setattr(session, "get", AsyncMock(return_value=object()))
        yield MemoryLedger(session)


def registry_for() -> tuple[
    ToolRegistry, FakeGitConnector, FakeCIConnector, FakeArgoCDConnector, FakeConfigCenterConnector
]:
    registry = ToolRegistry()
    git, ci, argo, config = (
        FakeGitConnector(),
        FakeCIConnector(),
        FakeArgoCDConnector(),
        FakeConfigCenterConnector(),
    )
    register_change_tools(registry, git, ci, argo, config)
    return registry, git, ci, argo, config


def test_l0_schemas_and_duplicate_registration() -> None:
    registry, git, ci, argo, config = registry_for()
    assert {item.name for item in registry.declarations()} == set(PARAMS)
    for item in registry.declarations():
        assert item.risk_level is RiskLevel.L0
        assert item.input_schema["additionalProperties"] is False
        assert item.output_schema["additionalProperties"] is False
    with pytest.raises(DuplicateTool):
        register_change_tools(registry, git, ci, argo, config)


@pytest.mark.parametrize("tool", PARAMS)
@pytest.mark.asyncio
async def test_step14_acceptance_exact_evidence_and_audit(ledger: MemoryLedger, tool: str) -> None:
    registry, *_ = registry_for()
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name=tool,
        parameters=PARAMS[tool],
        actor="fake-agent",
    )
    assert result.status is DispatchStatus.SUCCEEDED and result.result is not None
    assert len(ledger.evidence) == len(ledger.audits) == 1
    assert result.evidence_id == ledger.evidence[0].id == ledger.audits[0].evidence_id
    assert ledger.evidence[0].source_tool == tool
    assert ledger.evidence[0].result_snapshot == result.result
    if tool == "compare_versions":
        config = result.result["configuration"]
        assert isinstance(config, dict)
        assert config["changes"] == [
            {"key": "db.pool.max_connections", "before": "50", "after": "500"}
        ]
        code = result.result["code"]
        assert isinstance(code, dict) and "-db.pool.max_connections: 50" in str(code["files"])
        assert "+db.pool.max_connections: 500" in str(code["files"])
    else:
        for key in ("deployments", "builds"):
            records = result.result[key]
            assert isinstance(records, list)
            assert [r["id"] for r in records if isinstance(r, dict)] == ["37", "36"]
        assert result.result["history_scope"] == "source_retained_history"


@pytest.mark.parametrize("tool", PARAMS)
@pytest.mark.asyncio
async def test_replay_with_closed_sources_does_not_read(ledger: MemoryLedger, tool: str) -> None:
    registry, *connectors = registry_for()
    dispatcher = ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger)
    task = uuid4()
    live = await dispatcher.dispatch(
        task_id=task, tool_name=tool, parameters=PARAMS[tool], actor="AI"
    )
    for connector in connectors:
        await connector.aclose()
    replay = await dispatcher.dispatch(
        task_id=task,
        tool_name=tool,
        parameters=PARAMS[tool],
        actor="replay-agent",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=ledger.evidence[0].collected_at + timedelta(seconds=1),
    )
    assert replay.status is DispatchStatus.REPLAYED and replay.result == live.result
    assert replay.evidence_id == live.evidence_id
    assert len(ledger.evidence) == 1 and len(ledger.audits) == 2


@pytest.mark.parametrize("tool", PARAMS)
@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.NEED_APPROVAL])
@pytest.mark.asyncio
async def test_policy_blocks_before_any_read(
    ledger: MemoryLedger, tool: str, decision: PolicyDecision
) -> None:
    registry, *connectors = registry_for()
    for connector in connectors:
        await connector.aclose()
    policy = PolicyEngine(
        PolicyEnvironment.TEST,
        PolicyConfig(
            rules=(
                PolicyRule(
                    id="changes-gate",
                    action_names=(tool,),
                    risk_levels=(RiskLevel.L0,),
                    decision=decision,
                    reason="专项验收",
                ),
            )
        ),
    )
    result = await ToolDispatcher(registry, policy, ledger).dispatch(
        task_id=uuid4(),
        tool_name=tool,
        parameters=PARAMS[tool],
        actor="AI",
    )
    assert result.status is DispatchStatus.REJECTED
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize("tool", PARAMS)
@pytest.mark.parametrize("extra", ["base_url", "token", "approved", "risk_level", "repository"])
@pytest.mark.asyncio
async def test_untrusted_connection_parameters_rejected(
    ledger: MemoryLedger, tool: str, extra: str
) -> None:
    registry, *_ = registry_for()
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name=tool,
        parameters={**PARAMS[tool], extra: "untrusted"},
        actor="AI",
    )
    assert result.status is DispatchStatus.REJECTED and result.error_code == "invalid_parameters"
    assert ledger.evidence == []


@pytest.mark.parametrize("tool", PARAMS)
@pytest.mark.asyncio
async def test_failure_has_no_success_evidence(ledger: MemoryLedger, tool: str) -> None:
    registry, *connectors = registry_for()
    for connector in connectors:
        await connector.aclose()
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name=tool,
        parameters=PARAMS[tool],
        actor="AI",
    )
    assert result.status is DispatchStatus.FAILED and result.error_code == "tool_failed"
    assert ledger.evidence == [] and len(ledger.audits) == 1
