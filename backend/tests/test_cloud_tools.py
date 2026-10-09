"""get_cloud_resources 经 Dispatcher 的 Evidence、审计、Policy 与离线 Replay。"""

from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.cloud.fake import SAMPLE_END, SAMPLE_START, FakeCloudConnector
from app.connectors.cloud.models import CloudQuery
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyConfig, PolicyDecision, PolicyEnvironment, PolicyRule, RiskLevel
from app.tools.cloud import register_cloud_tools
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus, JsonObject
from app.tools.registry import DuplicateTool, ToolRegistry
from tests.test_tools import MemoryLedger

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
PARAMETERS: JsonObject = {
    "service_name": "payment-service",
    "start": "2026-10-01T01:00:00Z",
    "end": "2026-10-01T01:10:00Z",
}


@pytest_asyncio.fixture
async def ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MemoryLedger]:
    async with AsyncSession() as session, session.begin():
        monkeypatch.setattr(session, "get", AsyncMock(return_value=object()))
        yield MemoryLedger(session)


def registry_for(reader: FakeCloudConnector) -> ToolRegistry:
    registry = ToolRegistry()
    register_cloud_tools(registry, reader)
    return registry


def test_l0_schema_and_duplicate_registration() -> None:
    reader = FakeCloudConnector()
    registry = registry_for(reader)
    (declaration,) = registry.declarations()
    assert declaration.name == "get_cloud_resources" and declaration.risk_level is RiskLevel.L0
    assert declaration.input_schema["additionalProperties"] is False
    assert declaration.output_schema["additionalProperties"] is False
    with pytest.raises(DuplicateTool):
        register_cloud_tools(registry, reader)


@pytest.mark.parametrize("explicit_window", [True, False])
@pytest.mark.asyncio
async def test_step15_acceptance_exact_evidence_audit_and_replay(
    ledger: MemoryLedger,
    explicit_window: bool,
) -> None:
    reader = FakeCloudConnector()
    dispatcher = ToolDispatcher(registry_for(reader), PolicyEngine(PolicyEnvironment.TEST), ledger)
    task = uuid4()
    parameters: JsonObject = PARAMETERS if explicit_window else {"service_name": "payment-service"}
    live = await dispatcher.dispatch(
        task_id=task, tool_name="get_cloud_resources", parameters=parameters, actor="fake-agent"
    )
    assert live.status is DispatchStatus.SUCCEEDED and live.result is not None
    assert len(ledger.evidence) == len(ledger.audits) == 1
    assert live.evidence_id == ledger.evidence[0].id == ledger.audits[0].evidence_id
    assert ledger.evidence[0].source_tool == "get_cloud_resources"
    assert ledger.evidence[0].result_snapshot == live.result
    records = live.result["resources"]
    assert isinstance(records, list) and len(records) == 8
    rds = next(r for r in records if isinstance(r, dict) and r["product"] == "rds")
    assert isinstance(rds, dict) and isinstance(rds["rds_connections"], dict)
    assert rds["rds_connections"]["total_connections"] == 520.0
    await reader.aclose()
    replay = await dispatcher.dispatch(
        task_id=task,
        tool_name="get_cloud_resources",
        parameters=parameters,
        actor="replay-agent",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=ledger.evidence[0].collected_at + timedelta(seconds=1),
    )
    assert replay.status is DispatchStatus.REPLAYED and replay.result == live.result
    assert replay.evidence_id == live.evidence_id
    assert len(ledger.evidence) == 1 and len(ledger.audits) == 2


@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.NEED_APPROVAL])
@pytest.mark.asyncio
async def test_policy_blocks_before_cloud_read(
    ledger: MemoryLedger, decision: PolicyDecision
) -> None:
    reader = FakeCloudConnector()
    await reader.aclose()
    policy = PolicyEngine(
        PolicyEnvironment.TEST,
        PolicyConfig(
            rules=(
                PolicyRule(
                    id="cloud-gate",
                    action_names=("get_cloud_resources",),
                    risk_levels=(RiskLevel.L0,),
                    decision=decision,
                    reason="专项验收",
                ),
            )
        ),
    )
    result = await ToolDispatcher(registry_for(reader), policy, ledger).dispatch(
        task_id=uuid4(),
        tool_name="get_cloud_resources",
        parameters=PARAMETERS,
        actor="AI",
    )
    assert result.status is DispatchStatus.REJECTED
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize(
    "extra",
    [
        "base_url",
        "endpoints",
        "token",
        "access_key_secret",
        "region_id",
        "resource_id",
        "action",
        "approved",
        "risk_level",
    ],
)
@pytest.mark.asyncio
async def test_untrusted_parameters_rejected(ledger: MemoryLedger, extra: str) -> None:
    result = await ToolDispatcher(
        registry_for(FakeCloudConnector()), PolicyEngine(PolicyEnvironment.TEST), ledger
    ).dispatch(
        task_id=uuid4(),
        tool_name="get_cloud_resources",
        parameters={**PARAMETERS, extra: "untrusted"},
        actor="AI",
    )
    assert result.status is DispatchStatus.REJECTED and result.error_code == "invalid_parameters"
    assert ledger.evidence == []


@pytest.mark.asyncio
async def test_source_failure_writes_no_success_evidence(ledger: MemoryLedger) -> None:
    reader = FakeCloudConnector()
    await reader.aclose()
    result = await ToolDispatcher(
        registry_for(reader), PolicyEngine(PolicyEnvironment.TEST), ledger
    ).dispatch(
        task_id=uuid4(),
        tool_name="get_cloud_resources",
        parameters=PARAMETERS,
        actor="AI",
    )
    assert result.status is DispatchStatus.FAILED and result.error_code == "tool_failed"
    assert ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.parametrize("wrong_service", [True, False])
@pytest.mark.asyncio
async def test_wrong_source_scope_is_not_recorded_as_evidence(
    ledger: MemoryLedger,
    monkeypatch: pytest.MonkeyPatch,
    wrong_service: bool,
) -> None:
    reader = FakeCloudConnector()
    snapshot = await reader.get_cloud_resources(
        CloudQuery(
            service_name="payment-service",
            start=SAMPLE_START,
            end=SAMPLE_END,
        )
    )
    wrong = snapshot.model_copy(
        update={"service_name": "other-service"}
        if wrong_service
        else {"start": SAMPLE_START - timedelta(minutes=1)}
    )
    monkeypatch.setattr(reader, "get_cloud_resources", AsyncMock(return_value=wrong))
    result = await ToolDispatcher(
        registry_for(reader), PolicyEngine(PolicyEnvironment.TEST), ledger
    ).dispatch(
        task_id=uuid4(),
        tool_name="get_cloud_resources",
        parameters=PARAMETERS,
        actor="AI",
    )
    assert result.status is DispatchStatus.FAILED
    assert ledger.evidence == [] and len(ledger.audits) == 1
