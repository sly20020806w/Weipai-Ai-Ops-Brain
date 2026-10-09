"""独立临时 PostgreSQL 库：证据引用、只追加保护和任务审计原子性。"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, event, insert, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Mapper

from app.config import parse_database_url
from app.db.session import Database
from app.ledger.models import AppendOnlyViolation, AuditEventType, AuditRecord, Evidence
from app.ledger.service import EvidenceNotFound, LedgerService
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import InvalidTaskTransition, TaskSource, TaskStatus
from tests.database_support import get_test_database_url, migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-db.ps1 进行本地 PostgreSQL 验收"
)


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        yield instance
    finally:
        await instance.dispose()


async def create_task(database: Database) -> UUID:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="本地证据验收", reason="Fake 数据验收"
        )
        return task.id


async def create_records(database: Database) -> tuple[UUID, UUID, UUID]:
    task_id = await create_task(database)
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        evidence = await ledger.append_evidence(
            task_id=task_id,
            source_tool="query_metrics",
            parameters={"service": "payment-service"},
            result_snapshot={"error_rate": 0.05},
        )
        audit = await ledger.append_audit(
            task_id=task_id,
            event_type=AuditEventType.TOOL_CALL,
            actor="fake-agent",
            operation="query_metrics",
            outcome="succeeded",
            details={"risk_level": "L0"},
            evidence_id=evidence.id,
        )
        return task_id, evidence.id, audit.id


@pytest.mark.asyncio
async def test_evidence_cross_session_time_order_task_filter_and_exact_id(
    database: Database,
) -> None:
    task_id = await create_task(database)
    other_task = await create_task(database)
    start = datetime(2026, 10, 6, 8, tzinfo=timezone(timedelta(hours=8)))
    records: list[tuple[datetime, UUID]] = []
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        for minute in (2, 0, 1, 1):
            record = await ledger.append_evidence(
                task_id=task_id,
                source_tool="query_logs",
                parameters={"minute": minute},
                result_snapshot={"messages": ["Fake 日志"]},
                source_reference=f"fake://logs/{minute}",
                collected_at=start + timedelta(minutes=minute),
            )
            records.append((record.collected_at, record.id))
        await ledger.append_evidence(
            task_id=other_task,
            source_tool="query_traces",
            parameters={},
            source_reference="fake://trace/T2",
        )
    async with database.session() as session:
        ledger = LedgerService(session)
        loaded = await ledger.evidence_for_task(task_id)
        assert [(row.collected_at, row.id) for row in loaded] == sorted(records)
        for row in loaded:
            assert (await ledger.get_evidence(row.id)).id == row.id
            assert row.task_id == task_id and row.collected_at.tzinfo is UTC
            assert row.created_at.tzinfo is UTC and row.updated_at.tzinfo is UTC
            assert row.parameters["minute"] in {0, 1, 2}
            assert row.result_snapshot == {"messages": ["Fake 日志"]}
        reference = (await ledger.evidence_for_task(other_task))[0]
        assert reference.result_snapshot is None and reference.source_reference == "fake://trace/T2"
        assert await ledger.evidence_for_task(uuid4()) == []
        with pytest.raises(EvidenceNotFound):
            await ledger.get_evidence(uuid4())


@pytest.mark.asyncio
async def test_audit_all_types_reference_and_time_order(database: Database) -> None:
    task_id, evidence_id, _ = await create_records(database)
    other_task = await create_task(database)
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        for event_type in (
            AuditEventType.APPROVAL,
            AuditEventType.EXECUTION,
            AuditEventType.HUMAN_INTERACTION,
        ):
            await ledger.append_audit(
                task_id=task_id,
                event_type=event_type,
                actor="local-tester",
                operation=f"fake.{event_type.value}",
                outcome="denied",
                details={"fake": True},
            )
    async with database.session() as session:
        rows = await LedgerService(session).audits_for_task(task_id)
        assert {row.event_type for row in rows} == set(AuditEventType)
        assert len(rows) == len(AuditEventType) and all(row.task_id == task_id for row in rows)
        assert [(row.occurred_at, row.id) for row in rows] == sorted(
            (row.occurred_at, row.id) for row in rows
        )
        assert all(row.occurred_at.tzinfo is UTC and row.created_at.tzinfo is UTC for row in rows)
        tool = next(row for row in rows if row.event_type is AuditEventType.TOOL_CALL)
        assert tool.evidence_id == evidence_id and tool.actor == "fake-agent"
        assert len(await LedgerService(session).audits_for_task(other_task)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [Evidence, AuditRecord])
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_orm_update_and_delete_are_rejected(
    database: Database, model: type[Evidence] | type[AuditRecord], operation: str
) -> None:
    _, evidence_id, audit_id = await create_records(database)
    record_id = evidence_id if model is Evidence else audit_id
    with pytest.raises(AppendOnlyViolation):
        async with database.session() as session, session.begin():
            record = await session.get(model, record_id)
            assert record is not None
            if operation == "delete":
                await session.delete(record)
            else:
                record.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
            await session.flush()
    async with database.session() as session:
        record = await session.get(model, record_id)
        assert record is not None and record.updated_at.year != 2020


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [Evidence, AuditRecord])
@pytest.mark.parametrize("core_table", [False, True])
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_session_bulk_writes_are_rejected(
    database: Database, model: type[Evidence] | type[AuditRecord], core_table: bool, operation: str
) -> None:
    _, evidence_id, audit_id = await create_records(database)
    table = cast(Table, model.__table__)
    target = table if core_table else model
    record_id = evidence_id if model is Evidence else audit_id
    statement = (
        update(target).values(updated_at=datetime(2020, 1, 1, tzinfo=UTC))
        if operation == "update"
        else delete(target)
    ).where(table.c.id == record_id)
    async with database.session() as session, session.begin():
        with pytest.raises(AppendOnlyViolation):
            await session.execute(statement)


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["evidence_ledger", "audit_log"])
@pytest.mark.parametrize("operation", ["update", "delete", "truncate"])
@pytest.mark.parametrize("connection_path", ["session", "engine"])
async def test_raw_sql_cannot_modify_or_clear_ledger(
    database: Database, table: str, operation: str, connection_path: str
) -> None:
    task_id, _, _ = await create_records(database)
    # 表名只来自以上固定参数；TRUNCATE CASCADE 也不能绕过证据/审计保护。
    sql = {
        "update": f"UPDATE {table} SET updated_at = now() WHERE task_id = :task_id",
        "delete": f"DELETE FROM {table} WHERE task_id = :task_id",
        "truncate": f"TRUNCATE {table} CASCADE",
    }[operation]
    with pytest.raises(DBAPIError, match="append-only ledger"):
        if connection_path == "session":
            async with database.session() as session, session.begin():
                await session.execute(text(sql), {"task_id": task_id})
        else:
            async with database.engine.begin() as connection:
                await connection.execute(text(sql), {"task_id": task_id})
    async with database.session() as session:
        ledger = LedgerService(session)
        assert len(await ledger.evidence_for_task(task_id)) == 1
        assert len(await ledger.audits_for_task(task_id)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing_result", "array_parameters", "missing_task"])
async def test_database_rejects_invalid_evidence_even_without_service(
    database: Database, invalid: str
) -> None:
    task_id = await create_task(database)
    statement = insert(Evidence).values(
        task_id=uuid4() if invalid == "missing_task" else task_id,
        source_tool="fake.query",
        parameters=[] if invalid == "array_parameters" else {},
        result_snapshot=None if invalid == "missing_result" else {"fake": True},
    )
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            await session.execute(statement)


@pytest.mark.asyncio
async def test_audit_cannot_reference_another_tasks_evidence(database: Database) -> None:
    _, evidence_id, _ = await create_records(database)
    other_task = await create_task(database)
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            await LedgerService(session).append_audit(
                task_id=other_task,
                event_type=AuditEventType.TOOL_CALL,
                actor="fake-agent",
                operation="fake.query",
                outcome="succeeded",
                details={},
                evidence_id=evidence_id,
            )
    async with database.session() as session:
        assert len(await LedgerService(session).audits_for_task(other_task)) == 1


@pytest.mark.asyncio
async def test_outer_rollback_discards_evidence_and_audit_together(database: Database) -> None:
    task_id = await create_task(database)
    with pytest.raises(RuntimeError, match="回滚"):
        async with database.session() as session, session.begin():
            ledger = LedgerService(session)
            record = await ledger.append_evidence(
                task_id=task_id, source_tool="fake.query", parameters={}, result_snapshot={}
            )
            await ledger.append_audit(
                task_id=task_id,
                event_type=AuditEventType.TOOL_CALL,
                actor="tester",
                operation="fake.query",
                outcome="succeeded",
                details={},
                evidence_id=record.id,
            )
            raise RuntimeError("外层回滚")
    async with database.session() as session:
        ledger = LedgerService(session)
        assert await ledger.evidence_for_task(task_id) == []
        assert len(await ledger.audits_for_task(task_id)) == 1


@pytest.mark.asyncio
async def test_task_transitions_and_audit_are_one_atomic_operation(database: Database) -> None:
    task_id = await create_task(database)

    def reject_audit(
        mapper: Mapper[AuditRecord], connection: Connection, record: AuditRecord
    ) -> None:
        if record.details.get("reason") == "模拟审计失败":
            raise RuntimeError("模拟审计失败")

    event.listen(AuditRecord, "before_insert", reject_audit)
    try:
        async with database.session() as session, session.begin():
            with pytest.raises(RuntimeError, match="模拟审计失败"):
                await TaskService(session).transition(
                    task_id,
                    TaskStatus.CONTEXT_BUILDING,
                    expected_status=TaskStatus.NEW,
                    expected_version=0,
                    reason="模拟审计失败",
                )
        async with database.session() as session, session.begin():
            with pytest.raises(RuntimeError, match="模拟审计失败"):
                await TaskService(session).create(
                    source=TaskSource.HUMAN, title="审计失败不应创建任务", reason="模拟审计失败"
                )
    finally:
        event.remove(AuditRecord, "before_insert", reject_audit)
    async with database.session() as session, session.begin():
        assert (
            await session.scalar(select(AITask).where(AITask.title == "审计失败不应创建任务"))
            is None
        )
        task = await session.get(AITask, task_id)
        assert task is not None and task.status is TaskStatus.NEW and task.status_version == 0
        assert len(await TaskService(session).history(task_id)) == 1
        assert len(await LedgerService(session).audits_for_task(task_id)) == 1
        with pytest.raises(InvalidTaskTransition):
            await TaskService(session).transition(
                task_id,
                TaskStatus.RESOLVED,
                expected_status=TaskStatus.NEW,
                expected_version=0,
                reason="非法迁移",
            )
        await TaskService(session).transition(
            task_id,
            TaskStatus.CONTEXT_BUILDING,
            expected_status=TaskStatus.NEW,
            expected_version=0,
            reason="合法迁移",
        )
    async with database.session() as session:
        history = await TaskService(session).history(task_id)
        audits = await LedgerService(session).audits_for_task(task_id)
        assert len(history) == len(audits) == 2
        for previous, record in zip(history, audits, strict=True):
            assert record.event_type is AuditEventType.STATE_TRANSITION
            assert (
                record.occurred_at == previous.changed_at and record.actor == previous.actor.value
            )
            assert record.details == {
                "from_status": None if previous.from_status is None else previous.from_status.value,
                "to_status": previous.to_status.value,
                "status_version": previous.sequence,
                "reason": previous.reason,
            }


@pytest.mark.asyncio
async def test_schema_timestamps_and_append_only_triggers(database: Database) -> None:
    async with database.engine.connect() as connection:
        columns = (
            await connection.execute(
                text(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name IN ('evidence_ledger', 'audit_log')"
                )
            )
        ).all()
        assert {(row[0], row[1]) for row in columns if row[2] == "timestamp with time zone"} == {
            ("evidence_ledger", "created_at"),
            ("evidence_ledger", "updated_at"),
            ("evidence_ledger", "collected_at"),
            ("audit_log", "created_at"),
            ("audit_log", "updated_at"),
            ("audit_log", "occurred_at"),
        }
        triggers = (
            await connection.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname IN ('evidence_ledger_append_only', 'audit_log_append_only')"
                )
            )
        ).all()
        assert set(triggers) == {"evidence_ledger_append_only", "audit_log_append_only"}
