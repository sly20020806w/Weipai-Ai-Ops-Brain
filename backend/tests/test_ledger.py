"""Ledger 入参、时间、快照隔离和事务契约；不请求外部系统。"""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import EvidenceNotFound, LedgerService


@pytest.fixture
def session() -> AsyncMock:
    instance = AsyncMock(spec=AsyncSession)
    instance.in_transaction.return_value = True
    return instance


@pytest.mark.asyncio
async def test_snapshot_is_detached_and_time_is_normalized(session: AsyncMock) -> None:
    parameters: dict[str, JsonValue] = {"filters": {"service": "payment-service"}}
    snapshot: dict[str, JsonValue] = {"values": [1, 2]}
    record = await LedgerService(session).append_evidence(
        task_id=uuid4(),
        source_tool=" query_metrics ",
        parameters=parameters,
        result_snapshot=snapshot,
        collected_at=datetime(2026, 10, 6, 8, tzinfo=timezone(timedelta(hours=8))),
    )
    parameters.clear()
    snapshot.clear()
    assert record.parameters == {"filters": {"service": "payment-service"}}
    assert record.result_snapshot == {"values": [1, 2]}
    assert record.source_tool == "query_metrics"
    assert record.collected_at == datetime(2026, 10, 6, tzinfo=UTC)
    assert record.collected_at.tzinfo is UTC


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [False, 0, "", [], {}])
async def test_empty_json_results_are_valid(session: AsyncMock, snapshot: JsonValue) -> None:
    record = await LedgerService(session).append_evidence(
        task_id=uuid4(), source_tool="query_logs", parameters={}, result_snapshot=snapshot
    )
    assert record.result_snapshot == snapshot and record.collected_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_reference_only_evidence_and_exact_id_lookup(session: AsyncMock) -> None:
    service = LedgerService(session)
    record = await service.append_evidence(
        task_id=uuid4(),
        source_tool="query_traces",
        parameters={},
        source_reference=" fake://traces/T1 ",
    )
    assert record.result_snapshot is None and record.source_reference == "fake://traces/T1"
    session.get.return_value = record
    assert await service.get_evidence(record.id) is record
    session.get.assert_awaited_once_with(Evidence, record.id)
    session.get.return_value = None
    with pytest.raises(EvidenceNotFound):
        await service.get_evidence(uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", list(AuditEventType))
async def test_four_audit_types_include_actor_outcome_and_utc(
    session: AsyncMock, event_type: AuditEventType
) -> None:
    details: dict[str, JsonValue] = {"reason": "本地测试"}
    record = await LedgerService(session).append_audit(
        task_id=uuid4(),
        event_type=event_type,
        actor="tester",
        operation="fake.operation",
        outcome="succeeded",
        details=details,
    )
    details.clear()
    assert record.event_type is event_type and record.actor == "tester"
    assert record.operation == "fake.operation" and record.outcome == "succeeded"
    assert record.details == {"reason": "本地测试"} and record.occurred_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_explicit_transaction_required_for_both_writes(session: AsyncMock) -> None:
    session.in_transaction.return_value = False
    service = LedgerService(session)
    with pytest.raises(RuntimeError, match="开启事务"):
        await service.append_evidence(
            task_id=uuid4(), source_tool="query_logs", parameters={}, result_snapshot={}
        )
    with pytest.raises(RuntimeError, match="开启事务"):
        await service.append_audit(
            task_id=uuid4(),
            event_type=AuditEventType.APPROVAL,
            actor="tester",
            operation="fake.approve",
            outcome="denied",
            details={},
        )
    session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"source_tool": " \n"},
        {"source_tool": "x" * 201},
        {"result_snapshot": None},
        {"source_reference": " "},
        {"parameters": []},
        {"parameters": {1: "invalid-key"}},
        {"result_snapshot": (1, 2)},
        {"result_snapshot": float("nan")},
        {"result_snapshot": {"value": float("inf")}},
        {"collected_at": datetime(2026, 10, 6)},
    ],
)
async def test_invalid_evidence_is_rejected_before_write(
    session: AsyncMock, changes: dict[str, object]
) -> None:
    arguments = dict(task_id=uuid4(), source_tool="query_logs", parameters={}, result_snapshot={})
    arguments.update(changes)
    with pytest.raises(ValueError):
        await LedgerService(session).append_evidence(**arguments)  # type: ignore[arg-type]
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"actor": " "},
        {"operation": " "},
        {"outcome": " "},
        {"event_type": "invalid"},
        {"details": []},
        {"occurred_at": datetime(2026, 10, 6)},
    ],
)
async def test_invalid_audit_is_rejected_before_write(
    session: AsyncMock, changes: dict[str, object]
) -> None:
    arguments = dict(
        task_id=uuid4(),
        event_type=AuditEventType.TOOL_CALL,
        actor="tester",
        operation="query_logs",
        outcome="succeeded",
        details={},
    )
    arguments.update(changes)
    with pytest.raises(ValueError):
        await LedgerService(session).append_audit(**arguments)  # type: ignore[arg-type]
    session.add.assert_not_called()
