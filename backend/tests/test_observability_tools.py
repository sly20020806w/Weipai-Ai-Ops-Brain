"""三个 L0 查询的 Evidence、审计、时间边界、Policy 与 Replay 验收。"""

from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.observability.fake import (
    FakeARMSConnector,
    FakePrometheusConnector,
    FakeSLSConnector,
)
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyConfig, PolicyDecision, PolicyEnvironment, PolicyRule, RiskLevel
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus, JsonObject
from app.tools.observability import register_observability_tools
from app.tools.registry import DuplicateTool, ToolRegistry
from tests.test_tools import MemoryLedger

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
TOOLS = ["query_metrics", "query_logs", "query_traces"]
PARAMS: JsonObject = {
    "service_name": "payment-service",
    "start": "2026-10-01T01:00:00Z",
    "end": "2026-10-01T01:10:00Z",
}


@pytest_asyncio.fixture
async def ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MemoryLedger]:
    async with AsyncSession() as session, session.begin():
        monkeypatch.setattr(session, "get", AsyncMock(return_value=object()))
        yield MemoryLedger(session)


def registry_for() -> tuple[
    ToolRegistry, FakePrometheusConnector, FakeSLSConnector, FakeARMSConnector
]:
    registry = ToolRegistry()
    prom, sls, arms = FakePrometheusConnector(), FakeSLSConnector(), FakeARMSConnector()
    register_observability_tools(registry, prom, sls, arms)
    return registry, prom, sls, arms


def test_l0_strict_schemas_and_duplicate_registration() -> None:
    registry, prom, sls, arms = registry_for()
    assert {tool.name for tool in registry.declarations()} == set(TOOLS)
    for tool in registry.declarations():
        assert tool.risk_level is RiskLevel.L0
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["additionalProperties"] is False
        assert tool.input_schema["required"] == ["service_name", "start", "end"]
    with pytest.raises(DuplicateTool):
        register_observability_tools(registry, prom, sls, arms)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_exact_evidence_audit_and_only_in_window(ledger: MemoryLedger, tool: str) -> None:
    registry, *_ = registry_for()
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name=tool,
        parameters=PARAMS,
        actor="fake-agent",
    )
    assert result.status is DispatchStatus.SUCCEEDED and result.result is not None
    assert result.policy.risk_level is RiskLevel.L0
    assert len(ledger.evidence) == len(ledger.audits) == 1
    evidence, audit = ledger.evidence[0], ledger.audits[0]
    assert result.evidence_id == evidence.id == audit.evidence_id
    assert evidence.source_tool == tool and evidence.result_snapshot == result.result
    assert evidence.parameters["start"] == "2026-10-01T01:00:00Z"
    assert evidence.collected_at.tzinfo is UTC
    assert "mock-secret" not in str(result.result)
    records = result.result[
        {"query_metrics": "series", "query_logs": "logs", "query_traces": "traces"}[tool]
    ]
    assert isinstance(records, list) and len(records) == (1 if tool == "query_metrics" else 2)
    if tool == "query_metrics":
        series = records[0]
        assert isinstance(series, dict)
        records = series["points"]
        assert isinstance(records, list)
    assert all(isinstance(record, dict) for record in records)
    assert [r["timestamp"] for r in records if isinstance(r, dict)] == [
        "2026-10-01T01:00:00Z",
        "2026-10-01T01:05:00Z",
    ]
    if tool == "query_traces":
        edges = result.result["topology"]
        assert isinstance(edges, list) and len(edges) == 2
        assert all(isinstance(e, dict) and e["target_service"] == "payment-db" for e in edges)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_replay_after_close_preserves_original_evidence(
    ledger: MemoryLedger, tool: str
) -> None:
    registry, prom, sls, arms = registry_for()
    dispatcher = ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger)
    task_id = uuid4()
    live = await dispatcher.dispatch(task_id=task_id, tool_name=tool, parameters=PARAMS, actor="AI")
    assert live.status is DispatchStatus.SUCCEEDED
    for client in (prom, sls, arms):
        await client.aclose()
    replay = await dispatcher.dispatch(
        task_id=task_id,
        tool_name=tool,
        parameters=PARAMS,
        actor="replay-agent",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=ledger.evidence[0].collected_at + timedelta(seconds=1),
    )
    assert replay.status is DispatchStatus.REPLAYED
    assert replay.result == live.result and replay.evidence_id == live.evidence_id
    assert len(ledger.evidence) == 1 and len(ledger.audits) == 2


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.NEED_APPROVAL])
@pytest.mark.asyncio
async def test_policy_blocks_before_reading(
    ledger: MemoryLedger,
    tool: str,
    decision: PolicyDecision,
) -> None:
    registry, prom, sls, arms = registry_for()
    for client in (prom, sls, arms):
        await client.aclose()
    policy = PolicyEngine(
        PolicyEnvironment.TEST,
        PolicyConfig(
            rules=(
                PolicyRule(
                    id="observability-gate",
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
        parameters=PARAMS,
        actor="AI",
    )
    assert result.status is DispatchStatus.REJECTED
    assert result.error_code == (
        "policy_denied" if decision is PolicyDecision.DENY else "approval_required"
    )
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "changes",
    [
        {"start": "2026-10-01T01:00:00"},
        {"end": "2026-09-01T00:00:00Z"},
        {"service_name": 'x" or *'},
        {"base_url": "https://other.invalid"},
        {"token": "untrusted"},
        {"risk_level": "L0"},
        {"approved": True},
    ],
)
@pytest.mark.asyncio
async def test_invalid_query_cannot_read(
    ledger: MemoryLedger, tool: str, changes: JsonObject
) -> None:
    registry, prom, sls, arms = registry_for()
    for client in (prom, sls, arms):
        await client.aclose()
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name=tool,
        parameters={**PARAMS, **changes},
        actor="AI",
    )
    assert result.status is DispatchStatus.REJECTED and result.error_code == "invalid_parameters"
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_read_failure_does_not_make_success_evidence(ledger: MemoryLedger, tool: str) -> None:
    registry, prom, sls, arms = registry_for()
    for client in (prom, sls, arms):
        await client.aclose()
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name=tool,
        parameters=PARAMS,
        actor="AI",
    )
    assert result.status is DispatchStatus.FAILED and result.error_code == "tool_failed"
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_offset_query_normalized_for_evidence_and_replay(
    ledger: MemoryLedger, tool: str
) -> None:
    registry, *_ = registry_for()
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name=tool,
        parameters={
            **PARAMS,
            "start": "2026-10-01T09:00:00+08:00",
            "end": "2026-10-01T09:10:00+08:00",
        },
        actor="AI",
    )
    assert result.status is DispatchStatus.SUCCEEDED
    assert ledger.evidence[0].parameters["start"] == PARAMS["start"]
