"""Step 11：业务读取经唯一 Dispatcher 留下证据，Policy 和 Replay 仍生效。"""

from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.ops_platform.fake import FakeOpsPlatformConnector, sample_snapshot
from app.connectors.ops_platform.models import Owner, ServiceTreeNode
from app.policy.engine import PolicyEngine
from app.policy.models import (
    PolicyConfig,
    PolicyDecision,
    PolicyEnvironment,
    PolicyRule,
    RiskLevel,
)
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus, JsonObject
from app.tools.ops_platform import register_ops_platform_tools
from app.tools.registry import DuplicateTool, ToolRegistry
from tests.test_tools import MemoryLedger

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


@pytest_asyncio.fixture
async def ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MemoryLedger]:
    async with AsyncSession() as session, session.begin():
        monkeypatch.setattr(session, "get", AsyncMock(return_value=object()))
        yield MemoryLedger(session)


def registry_for(connector: FakeOpsPlatformConnector) -> ToolRegistry:
    registry = ToolRegistry()
    register_ops_platform_tools(registry, connector)
    return registry


def test_ops_tools_all_l0_with_schema_and_duplicate_rejected() -> None:
    connector = FakeOpsPlatformConnector()
    registry = registry_for(connector)
    declarations = registry.declarations()
    assert {tool.name for tool in declarations} == {
        "get_ops_service",
        "list_ops_services",
        "query_ops_tickets",
    }
    for tool in declarations:
        assert tool.risk_level is RiskLevel.L0
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["additionalProperties"] is False
    with pytest.raises(DuplicateTool):
        register_ops_platform_tools(registry, connector)


@pytest.mark.parametrize(
    "tool_name, params",
    [
        ("get_ops_service", {"service_name": "payment-service"}),
        ("list_ops_services", {"business_id": "payment"}),
        ("query_ops_tickets", {"service_name": "payment-service", "status": "open"}),
        ("query_ops_tickets", {"ticket_id": "TICKET-1001"}),
    ],
)
@pytest.mark.asyncio
async def test_ops_tool_exactly_one_evidence_and_audit(
    ledger: MemoryLedger, tool_name: str, params: JsonObject
) -> None:
    async with FakeOpsPlatformConnector() as connector:
        result = await ToolDispatcher(
            registry_for(connector), PolicyEngine(PolicyEnvironment.TEST), ledger
        ).dispatch(task_id=uuid4(), tool_name=tool_name, parameters=params, actor="fake-agent")
    assert result.status is DispatchStatus.SUCCEEDED
    assert result.policy.risk_level is RiskLevel.L0
    assert len(ledger.evidence) == len(ledger.audits) == 1
    assert result.evidence_id == ledger.evidence[0].id == ledger.audits[0].evidence_id
    assert ledger.evidence[0].result_snapshot == result.result
    assert ledger.evidence[0].source_tool == tool_name
    assert result.result is not None
    if tool_name == "get_ops_service":
        application = result.result["application"]
        assert isinstance(application, dict)
        assert application["business_id"] == "payment"
        assert result.result["business_path"] == [
            {"id": "weipai", "name": "微派（样例）", "parent_id": None},
            {"id": "payment", "name": "支付业务（样例）", "parent_id": "weipai"},
        ]
        assert result.result["owners"] == [
            {"id": "owner-payment", "name": "支付负责人（样例）", "team": "支付团队（样例）"}
        ]
    elif tool_name == "list_ops_services":
        applications = result.result["applications"]
        assert isinstance(applications, list) and len(applications) == 1
        assert isinstance(applications[0], dict)
        assert applications[0]["service_name"] == "payment-service"
    else:
        tickets = result.result["tickets"]
        assert isinstance(tickets, list) and len(tickets) == 1 and isinstance(tickets[0], dict)
        assert tickets[0]["id"] == "TICKET-1001"
        assert tickets[0]["created_at"] == "2026-10-01T01:00:00Z"


@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.NEED_APPROVAL])
@pytest.mark.asyncio
async def test_policy_gate_blocks_ops_connector_calls(
    ledger: MemoryLedger, decision: PolicyDecision
) -> None:
    connector = FakeOpsPlatformConnector()
    await connector.aclose()  # 若越过 Policy 调用则会失败，而不会返回 REJECTED。
    policy = PolicyEngine(
        PolicyEnvironment.TEST,
        PolicyConfig(
            rules=(
                PolicyRule(
                    id="ops-gate",
                    action_names=("get_ops_service",),
                    risk_levels=(RiskLevel.L0,),
                    decision=decision,
                    reason="专项门禁测试",
                ),
            )
        ),
    )
    result = await ToolDispatcher(registry_for(connector), policy, ledger).dispatch(
        task_id=uuid4(),
        tool_name="get_ops_service",
        parameters={"service_name": "payment-service"},
        actor="fake-agent",
    )
    assert result.status is DispatchStatus.REJECTED
    assert result.error_code == (
        "policy_denied" if decision is PolicyDecision.DENY else "approval_required"
    )
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize(
    "tool_name, params",
    [
        ("get_ops_service", {"service_name": "payment-service"}),
        ("list_ops_services", {}),
        ("query_ops_tickets", {"ticket_id": "TICKET-1001"}),
    ],
)
@pytest.mark.asyncio
async def test_replay_ops_snapshot_without_connector_calls(
    ledger: MemoryLedger, tool_name: str, params: JsonObject
) -> None:
    connector = FakeOpsPlatformConnector()
    dispatcher = ToolDispatcher(
        registry_for(connector), PolicyEngine(PolicyEnvironment.TEST), ledger
    )
    task_id = uuid4()
    live = await dispatcher.dispatch(
        task_id=task_id, tool_name=tool_name, parameters=params, actor="fake-agent"
    )
    assert live.status is DispatchStatus.SUCCEEDED
    await connector.aclose()
    replay = await dispatcher.dispatch(
        task_id=task_id,
        tool_name=tool_name,
        parameters=params,
        actor="fake-agent",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=ledger.evidence[0].collected_at + timedelta(seconds=1),
    )
    assert replay.status is DispatchStatus.REPLAYED
    assert replay.result == live.result and replay.evidence_id == live.evidence_id
    assert len(ledger.evidence) == 1 and len(ledger.audits) == 2


@pytest.mark.parametrize(
    "tool_name, params",
    [
        ("get_ops_service", {"service_name": "payment-service", "token": "cannot-pass-identity"}),
        (
            "get_ops_service",
            {"service_name": "payment-service", "base_url": "https://other.invalid"},
        ),
        ("query_ops_tickets", {"ticket_id": "TICKET-1001", "service_name": "payment-service"}),
        ("query_ops_tickets", {"status": ""}),
    ],
)
@pytest.mark.asyncio
async def test_ops_tool_invalid_inputs_never_read(
    ledger: MemoryLedger, tool_name: str, params: JsonObject
) -> None:
    connector = FakeOpsPlatformConnector()
    await connector.aclose()
    result = await ToolDispatcher(
        registry_for(connector), PolicyEngine(PolicyEnvironment.TEST), ledger
    ).dispatch(task_id=uuid4(), tool_name=tool_name, parameters=params, actor="fake-agent")
    assert result.status is DispatchStatus.REJECTED and result.error_code == "invalid_parameters"
    assert ledger.evidence == [] and len(ledger.audits) == 1


class ConflictingSource(FakeOpsPlatformConnector):
    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    async def list_service_tree(self) -> tuple[ServiceTreeNode, ...]:
        if self.kind == "cycle":
            return (ServiceTreeNode(id="payment", name="样例", parent_id="payment"),)
        if self.kind == "missing-business":
            return ()
        return await super().list_service_tree()

    async def list_owners(self, service_name: str) -> tuple[Owner, ...]:
        if self.kind == "wrong-owner":
            return (sample_snapshot().owners[1],)
        return await super().list_owners(service_name)


@pytest.mark.parametrize("kind", ["cycle", "missing-business", "wrong-owner"])
@pytest.mark.asyncio
async def test_conflicting_source_does_not_emit_service_evidence(
    ledger: MemoryLedger, kind: str
) -> None:
    async with ConflictingSource(kind) as connector:
        result = await ToolDispatcher(
            registry_for(connector), PolicyEngine(PolicyEnvironment.TEST), ledger
        ).dispatch(
            task_id=uuid4(),
            tool_name="get_ops_service",
            parameters={"service_name": "payment-service"},
            actor="fake-agent",
        )
    assert result.status is DispatchStatus.FAILED and result.error_code == "tool_failed"
    assert ledger.evidence == [] and len(ledger.audits) == 1
