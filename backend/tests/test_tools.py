"""Step 9：离线 Fake 调用、风险门禁、schema 与历史回放。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ConfigDict, JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utc_now
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.ledger.service import EvidenceNotFound, LedgerService
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyConfig, PolicyDecision, PolicyEnvironment, PolicyRule, RiskLevel
from app.tasks.service import TaskNotFound
from app.tools import DispatchMode, DispatchStatus, DuplicateTool, ToolDispatcher, ToolModel
from app.tools.models import JsonObject
from app.tools.registry import ToolRegistry, json_object

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


class ServiceInput(ToolModel):
    service: str
    limit: int = 2


class ServiceOutput(ToolModel):
    healthy: bool
    services: list[str]


class MemoryLedger(LedgerService):
    """仅测试使用；生产入口使用现有 SQLAlchemy LedgerService。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.evidence: list[Evidence] = []
        self.audits: list[AuditRecord] = []

    async def append_evidence(
        self,
        *,
        task_id: UUID,
        source_tool: str,
        parameters: dict[str, JsonValue],
        result_snapshot: JsonValue | None = None,
        source_reference: str | None = None,
        collected_at: datetime | None = None,
    ) -> Evidence:
        self._require_transaction()
        evidence = Evidence(
            id=uuid4(),
            task_id=task_id,
            source_tool=source_tool,
            parameters=json_object(parameters),
            result_snapshot=json_object(result_snapshot),
            source_reference=source_reference,
            collected_at=collected_at or utc_now(),
        )
        self.evidence.append(evidence)
        return evidence

    async def append_audit(
        self,
        *,
        task_id: UUID,
        event_type: AuditEventType,
        actor: str,
        operation: str,
        outcome: str,
        details: dict[str, JsonValue],
        evidence_id: UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditRecord:
        self._require_transaction()
        audit = AuditRecord(
            id=uuid4(),
            task_id=task_id,
            event_type=event_type,
            actor=actor,
            operation=operation,
            outcome=outcome,
            details=json_object(details),
            evidence_id=evidence_id,
            occurred_at=occurred_at or utc_now(),
        )
        self.audits.append(audit)
        return audit

    async def get_evidence(self, evidence_id: UUID) -> Evidence:
        for evidence in self.evidence:
            if evidence.id == evidence_id:
                return evidence
        raise EvidenceNotFound(str(evidence_id))

    async def audits_for_task(self, task_id: UUID) -> list[AuditRecord]:
        return [audit for audit in self.audits if audit.task_id == task_id]

    async def evidence_for_task(self, task_id: UUID) -> list[Evidence]:
        return [evidence for evidence in self.evidence if evidence.task_id == task_id]


class FakeTool:
    def __init__(self) -> None:
        self.calls: list[ServiceInput] = []

    async def __call__(self, parameters: ServiceInput) -> ServiceOutput:
        self.calls.append(parameters)
        return ServiceOutput(healthy=True, services=[parameters.service])


def registry_for(fake: FakeTool, risk: RiskLevel | None = RiskLevel.L0) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name="get_service_context",
        description="查询 Fake 服务上下文",
        input_model=ServiceInput,
        output_model=ServiceOutput,
        risk_level=risk,
        handler=fake,
    )
    return registry


@pytest_asyncio.fixture
async def ledger(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MemoryLedger]:
    async with AsyncSession() as session, session.begin():
        monkeypatch.setattr(session, "get", AsyncMock(return_value=object()))
        yield MemoryLedger(session)


def dispatcher(ledger: LedgerService, fake: FakeTool) -> ToolDispatcher:
    return ToolDispatcher(registry_for(fake), PolicyEngine(PolicyEnvironment.TEST), ledger)


def test_declarations_schema_risk_duplicate_and_copy() -> None:
    registry = registry_for(FakeTool(), None)
    declaration = registry.declarations()[0]
    assert declaration.risk_level is RiskLevel.L5
    assert declaration.input_schema["required"] == ["service"]
    assert declaration.output_schema["required"] == ["healthy", "services"]
    assert declaration.input_schema["additionalProperties"] is False
    declaration.input_schema.clear()
    assert registry.declarations()[0].input_schema
    for field_name in ("name", "risk_level"):
        with pytest.raises(ValidationError, match="frozen"):
            setattr(declaration, field_name, "L0")
    with pytest.raises(DuplicateTool):
        registry.register(
            name=declaration.name,
            description="重复声明",
            input_model=ServiceInput,
            output_model=ServiceOutput,
            handler=FakeTool(),
        )


@pytest.mark.parametrize("name", ["", "bad name", "with-hyphen", "a" * 65])
def test_invalid_tool_name_is_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        ToolRegistry().register(
            name=name,
            description="Fake",
            input_model=ServiceInput,
            output_model=ServiceOutput,
            handler=FakeTool(),
        )


@pytest.mark.asyncio
async def test_l0_exactly_one_evidence_one_audit_and_snapshot_isolation(
    ledger: MemoryLedger,
) -> None:
    fake = FakeTool()
    task_id = uuid4()
    result = await dispatcher(ledger, fake).dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="fake-agent",
    )
    assert result.status is DispatchStatus.SUCCEEDED
    assert result.result == {"healthy": True, "services": ["payment-service"]}
    assert len(fake.calls) == len(ledger.evidence) == len(ledger.audits) == 1
    evidence, audit = ledger.evidence[0], ledger.audits[0]
    assert evidence.id == audit.evidence_id == result.evidence_id
    assert audit.id == result.audit_id
    assert evidence.task_id == audit.task_id == task_id
    assert evidence.parameters == {"service": "payment-service", "limit": 2}
    assert evidence.collected_at.tzinfo is UTC
    assert audit.event_type is AuditEventType.TOOL_CALL
    assert audit.details["policy"] == result.policy.model_dump(mode="json")
    assert result.result is not None
    result.result["services"] = []
    assert evidence.result_snapshot == {"healthy": True, "services": ["payment-service"]}


@pytest.mark.parametrize("risk", [None, *list(RiskLevel)[1:]])
@pytest.mark.asyncio
async def test_unapproved_never_executes(ledger: MemoryLedger, risk: RiskLevel | None) -> None:
    fake = FakeTool()
    result = await ToolDispatcher(
        registry_for(fake, risk), PolicyEngine(PolicyEnvironment.TEST), ledger
    ).dispatch(task_id=uuid4(), tool_name="get_service_context", parameters={}, actor="fake-agent")
    assert result.status is DispatchStatus.REJECTED
    assert result.error_code == "approval_required"
    assert result.policy.risk_level is (risk or RiskLevel.L5)
    assert fake.calls == [] and ledger.evidence == []
    assert len(ledger.audits) == 1


@pytest.mark.parametrize("decision", list(PolicyDecision))
@pytest.mark.parametrize("risk", [RiskLevel.L0, RiskLevel.L1])
@pytest.mark.asyncio
async def test_policy_and_write_readiness_gates(
    ledger: MemoryLedger, risk: RiskLevel, decision: PolicyDecision
) -> None:
    fake = FakeTool()
    policy = PolicyEngine(
        PolicyEnvironment.PRODUCTION,
        PolicyConfig(
            rules=(
                PolicyRule(id="gate", risk_levels=(risk,), decision=decision, reason="门禁测试"),
            )
        ),
    )
    result = await ToolDispatcher(registry_for(fake, risk), policy, ledger).dispatch(
        task_id=uuid4(),
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="fake-agent",
    )
    assert result.policy.decision is decision
    allowed = decision is PolicyDecision.ALLOW and risk is RiskLevel.L0
    assert len(fake.calls) == len(ledger.evidence) == int(allowed)
    assert len(ledger.audits) == 1
    if decision is PolicyDecision.DENY:
        assert result.error_code == "policy_denied"
    if decision is PolicyDecision.ALLOW and risk is RiskLevel.L1:
        assert result.error_code == "write_execution_not_ready"


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"service": 1},
        {"service": "payment-service", "limit": "2"},
        {"service": "payment-service", "approved": True},
        {"service": "payment-service", "risk_level": "L0"},
        {"service": "payment-service", "limit": float("nan")},
    ],
)
@pytest.mark.asyncio
async def test_invalid_inputs_audited_without_execution(
    ledger: MemoryLedger, parameters: JsonObject
) -> None:
    fake = FakeTool()
    result = await dispatcher(ledger, fake).dispatch(
        task_id=uuid4(), tool_name="get_service_context", parameters=parameters, actor="fake-agent"
    )
    assert result.error_code == "invalid_parameters"
    assert fake.calls == [] and ledger.evidence == []
    assert len(ledger.audits) == 1


@pytest.mark.asyncio
async def test_tool_failure_is_sanitized(ledger: MemoryLedger) -> None:
    async def fail(parameters: ServiceInput) -> ServiceOutput:
        raise RuntimeError("secret-token")

    registry = ToolRegistry()
    registry.register(
        name="query_logs",
        description="Fake 故障",
        input_model=ServiceInput,
        output_model=ServiceOutput,
        risk_level=RiskLevel.L0,
        handler=fail,
    )
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name="query_logs",
        parameters={"service": "payment-service"},
        actor="AI",
    )
    assert result.status is DispatchStatus.FAILED
    assert ledger.evidence == [] and len(ledger.audits) == 1
    assert "secret-token" not in result.model_dump_json()
    assert "secret-token" not in str(ledger.audits[0].details)


@pytest.mark.parametrize("constructed_model", [False, True])
@pytest.mark.asyncio
async def test_invalid_output_does_not_become_evidence(
    ledger: MemoryLedger, constructed_model: bool
) -> None:
    async def invalid(parameters: ServiceInput) -> ServiceOutput:
        if constructed_model:
            # 故意绕过构造校验，确认 Dispatcher 会重新验证模型实例。
            return ServiceOutput.model_construct(healthy="yes", services=[])  # type: ignore[arg-type]
        return cast(ServiceOutput, {"healthy": "yes", "services": []})

    registry = ToolRegistry()
    registry.register(
        name="query_logs",
        description="Fake 无效返回",
        input_model=ServiceInput,
        output_model=ServiceOutput,
        risk_level=RiskLevel.L0,
        handler=invalid,
    )
    result = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=uuid4(),
        tool_name="query_logs",
        parameters={"service": "payment-service"},
        actor="AI",
    )
    assert result.error_code == "tool_failed" and ledger.evidence == []


@pytest.mark.asyncio
async def test_unknown_tool_and_missing_task_do_not_execute(
    ledger: MemoryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTool()
    entry = dispatcher(ledger, fake)
    result = await entry.dispatch(task_id=uuid4(), tool_name="unknown", parameters={}, actor="AI")
    assert result.error_code == "tool_not_found" and result.policy.risk_level is RiskLevel.L5
    monkeypatch.setattr(ledger.session, "get", AsyncMock(return_value=None))
    with pytest.raises(TaskNotFound):
        await entry.dispatch(
            task_id=uuid4(), tool_name="get_service_context", parameters={}, actor="AI"
        )
    assert fake.calls == [] and ledger.evidence == [] and len(ledger.audits) == 1


@pytest.mark.asyncio
async def test_missing_transaction_rejected_before_tool() -> None:
    async with AsyncSession() as session:
        fake = FakeTool()
        with pytest.raises(RuntimeError, match="开启事务"):
            await dispatcher(MemoryLedger(session), fake).dispatch(
                task_id=uuid4(), tool_name="get_service_context", parameters={}, actor="AI"
            )
        assert fake.calls == []


@pytest.mark.asyncio
async def test_replay_same_result_original_evidence_and_no_invocation(ledger: MemoryLedger) -> None:
    fake = FakeTool()
    entry = dispatcher(ledger, fake)
    task_id = uuid4()
    live = await entry.dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="AI",
    )
    replay = await entry.dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters={"service": "payment-service", "limit": 2},
        actor="replay-agent",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=utc_now(),
    )
    assert replay.status is DispatchStatus.REPLAYED
    assert replay.result == live.result and replay.evidence_id == live.evidence_id
    assert len(fake.calls) == len(ledger.evidence) == 1 and len(ledger.audits) == 2
    assert ledger.audits[-1].details["mode"] == "replay"


@pytest.mark.parametrize(
    "case,code",
    [
        ("missing_id", "invalid_replay"),
        ("missing_cutoff", "invalid_replay"),
        ("naive_time", "invalid_replay"),
        ("unknown_id", "replay_evidence_not_found"),
        ("other_task", "replay_mismatch"),
        ("parameters", "replay_mismatch"),
        ("future", "replay_mismatch"),
        ("tool", "replay_mismatch"),
        ("no_audit", "replay_missing_success_audit"),
        ("snapshot", "replay_invalid_snapshot"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_replay_never_falls_back_to_live(
    ledger: MemoryLedger, case: str, code: str
) -> None:
    fake = FakeTool()
    entry = dispatcher(ledger, fake)
    task_id = uuid4()
    live = await entry.dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="AI",
    )
    evidence = ledger.evidence[0]
    parameters: JsonObject = {"service": "payment-service"}
    evidence_id = live.evidence_id
    cutoff: datetime | None = utc_now()
    if case == "missing_id":
        evidence_id = None
    elif case == "unknown_id":
        evidence_id = uuid4()
    elif case == "missing_cutoff":
        cutoff = None
    elif case == "naive_time":
        cutoff = datetime(2026, 10, 6)
    elif case == "other_task":
        task_id = uuid4()
    elif case == "parameters":
        parameters["service"] = "other-service"
    elif case == "future":
        cutoff = evidence.collected_at - timedelta(seconds=1)
    elif case == "tool":
        evidence.source_tool = "query_logs"
    elif case == "no_audit":
        ledger.audits.clear()
    elif case == "snapshot":
        evidence.result_snapshot = None
    result = await entry.dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters=parameters,
        actor="AI",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=evidence_id,
        replay_before=cutoff,
    )
    assert result.status is DispatchStatus.REJECTED and result.error_code == code
    assert len(fake.calls) == len(ledger.evidence) == 1
    assert ledger.audits[-1].outcome == "rejected"


@pytest.mark.asyncio
async def test_live_mode_rejects_replay_arguments(ledger: MemoryLedger) -> None:
    fake = FakeTool()
    result = await dispatcher(ledger, fake).dispatch(
        task_id=uuid4(),
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="AI",
        replay_evidence_id=uuid4(),
    )
    assert result.error_code == "invalid_replay" and fake.calls == []


def test_registration_rejects_relaxed_schema_and_sync_implementation() -> None:
    class LooseInput(ServiceInput):
        model_config = ConfigDict(extra="allow")

    async def loose(parameters: LooseInput) -> ServiceOutput:
        return ServiceOutput(healthy=True, services=[])

    with pytest.raises(TypeError, match="严格校验"):
        ToolRegistry().register(
            name="loose",
            description="Fake",
            input_model=LooseInput,
            output_model=ServiceOutput,
            handler=loose,
        )

    def sync(parameters: ServiceInput) -> ServiceOutput:
        raise AssertionError("注册时不能执行同步函数")

    with pytest.raises(TypeError, match="异步函数"):
        ToolRegistry().register(
            name="sync",
            description="Fake",
            input_model=ServiceInput,
            output_model=ServiceOutput,
            handler=sync,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_replay_preserves_snapshot_when_current_schema_has_new_default(
    ledger: MemoryLedger,
) -> None:
    task_id = uuid4()
    live = await dispatcher(ledger, FakeTool()).dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="AI",
    )

    class ExpandedOutput(ServiceOutput):
        note: str = "新版本默认值"

    async def must_not_run(parameters: ServiceInput) -> ExpandedOutput:
        raise AssertionError("Replay 不能执行实现")

    registry = ToolRegistry()
    registry.register(
        name="get_service_context",
        description="Fake 升级后 schema",
        input_model=ServiceInput,
        output_model=ExpandedOutput,
        handler=must_not_run,
        risk_level=RiskLevel.L0,
    )
    replay = await ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger).dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="AI",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=utc_now(),
    )
    assert replay.status is DispatchStatus.REPLAYED and replay.result == live.result
    assert replay.result is not None and "note" not in replay.result


@pytest.mark.asyncio
async def test_json_timestamp_round_trip_in_live_and_replay(ledger: MemoryLedger) -> None:
    class Timestamp(ToolModel):
        observed_at: datetime

    calls: list[datetime] = []

    async def echo(parameters: Timestamp) -> Timestamp:
        calls.append(parameters.observed_at)
        return parameters

    registry = ToolRegistry()
    registry.register(
        name="get_observation",
        description="Fake 时间",
        input_model=Timestamp,
        output_model=Timestamp,
        handler=echo,
        risk_level=RiskLevel.L0,
    )
    task_id = uuid4()
    entry = ToolDispatcher(registry, PolicyEngine(PolicyEnvironment.TEST), ledger)
    parameters: JsonObject = {"observed_at": "2026-10-06T08:00:00Z"}
    live = await entry.dispatch(
        task_id=task_id,
        tool_name="get_observation",
        parameters=parameters,
        actor="AI",
    )
    replay = await entry.dispatch(
        task_id=task_id,
        tool_name="get_observation",
        parameters=parameters,
        actor="AI",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=utc_now(),
    )
    assert live.status is DispatchStatus.SUCCEEDED and replay.status is DispatchStatus.REPLAYED
    assert replay.result == live.result == parameters and len(calls) == 1


@pytest.mark.asyncio
async def test_replay_obeys_current_policy(ledger: MemoryLedger) -> None:
    fake = FakeTool()
    task_id = uuid4()
    live = await dispatcher(ledger, fake).dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="AI",
    )
    policy = PolicyEngine(
        PolicyEnvironment.TEST,
        PolicyConfig(
            rules=(
                PolicyRule(
                    id="deny-replay",
                    risk_levels=(RiskLevel.L0,),
                    decision=PolicyDecision.DENY,
                    reason="当前权限已收紧",
                ),
            )
        ),
    )
    replay = await ToolDispatcher(registry_for(fake), policy, ledger).dispatch(
        task_id=task_id,
        tool_name="get_service_context",
        parameters={"service": "payment-service"},
        actor="AI",
        mode=DispatchMode.REPLAY,
        replay_evidence_id=live.evidence_id,
        replay_before=utc_now(),
    )
    assert replay.error_code == "policy_denied" and len(fake.calls) == 1
    assert len(ledger.evidence) == 1 and len(ledger.audits) == 2
