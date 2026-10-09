"""只由 check-db.ps1 提供新建的本地临时库；普通检查不要求 Docker。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, String, inspect, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Mapped, mapped_column

from app.config import parse_database_url
from app.db.base import Base
from app.db.session import Database
from tests.database_support import get_test_database_url, migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-db.ps1 进行本地 PostgreSQL 验收"
)


class ProbeRecord(Base):
    # 验收模型仅存在于测试元数据，绝不进入应用迁移。
    metadata = MetaData()
    __tablename__ = "database_probe"
    name: Mapped[str] = mapped_column(String(100))


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        async with instance.engine.begin() as connection:
            await connection.run_sync(ProbeRecord.metadata.create_all)
        yield instance
    finally:
        try:
            async with instance.engine.begin() as connection:
                await connection.run_sync(ProbeRecord.metadata.drop_all)
        finally:
            await instance.dispose()


@pytest.mark.asyncio
async def test_uuid_utc_roundtrip_and_updated_at(database: Database) -> None:
    async with database.session() as session, session.begin():
        record = ProbeRecord(name="initial")
        session.add(record)
        await session.flush()
        record_id = record.id
        created_at = record.created_at
        updated_at = record.updated_at
        assert isinstance(record_id, UUID)
    async with database.session() as session, session.begin():
        loaded = await session.get(ProbeRecord, record_id)
        assert loaded is not None and loaded.name == "initial"
        assert loaded.created_at.tzinfo is UTC
        assert loaded.updated_at.tzinfo is UTC
        assert loaded.created_at == created_at
        loaded.name = "updated"
    async with database.session() as session:
        loaded = await session.get(ProbeRecord, record_id)
        assert loaded is not None and loaded.name == "updated"
        assert loaded.created_at == created_at
        assert loaded.updated_at > updated_at
        assert loaded.updated_at.tzinfo is UTC
        assert await session.scalar(text("SHOW timezone")) == "UTC"
        columns = await session.connection()
        rows = await columns.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'database_probe'"
            )
        )
        types = {str(row[0]): str(row[1]) for row in rows}
        assert types["id"] == "uuid"
        assert types["created_at"] == types["updated_at"] == "timestamp with time zone"


@pytest.mark.asyncio
async def test_offset_conversion_and_naive_time_rejection(database: Database) -> None:
    offset_time = datetime(2026, 10, 6, 8, tzinfo=timezone(timedelta(hours=8)))
    async with database.session() as session, session.begin():
        record = ProbeRecord(name="offset", created_at=offset_time, updated_at=offset_time)
        session.add(record)
        await session.flush()
        record_id = record.id
    async with database.session() as session:
        loaded = await session.get(ProbeRecord, record_id)
        assert loaded is not None
        assert loaded.created_at == datetime(2026, 10, 6, tzinfo=UTC)
        assert loaded.created_at.tzinfo is UTC
    with pytest.raises(StatementError, match="带时区"):
        async with database.session() as session, session.begin():
            session.add(ProbeRecord(name="naive", created_at=datetime(2026, 10, 6)))
    async with database.session() as session:
        assert await session.scalar(select(ProbeRecord).where(ProbeRecord.name == "naive")) is None


@pytest.mark.asyncio
async def test_uncommitted_and_failed_transactions_are_rolled_back(database: Database) -> None:
    async with database.session() as session:
        session.add(ProbeRecord(name="uncommitted"))
        await session.flush()
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.session() as session, session.begin():
            session.add(ProbeRecord(name="failed"))
            await session.flush()
            raise RuntimeError("rollback")
    async with database.session() as session:
        assert (await session.scalars(select(ProbeRecord))).all() == []


def test_alembic_upgrade_downgrade_on_empty_database() -> None:
    async def revision(expected_tables: list[str]) -> str | None:
        instance = Database(parse_database_url(get_test_database_url()))
        try:
            async with instance.engine.connect() as connection:
                tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
                assert sorted(tables) == sorted(expected_tables)
                value = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                assert value is None or isinstance(value, str)
                return value
        finally:
            await instance.dispose()

    migrate("upgrade", "head")
    task_tables = ["ai_task_status_history", "ai_tasks", "alembic_version"]
    ledger_tables = [*task_tables, "evidence_ledger", "audit_log"]
    graph_tables = [*ledger_tables, "context_graph_nodes", "context_graph_edges"]
    timeline_tables = [*graph_tables, "change_events"]
    knowledge_tables = [*timeline_tables, "knowledge_entries"]
    event_tables = [*knowledge_tables, "ops_events"]
    detection_tables = [*event_tables, "detection_cursors"]
    runbook_tables = [*detection_tables, "runbooks"]
    head_tables = [
        *runbook_tables,
        "human_knowledge_drafts",
        "inspection_risks",
        "auth_sessions",
        "auth_login_guard",
        "catalog_audit_log",
    ]
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("downgrade", "0010_runbook_engine")
    assert asyncio.run(revision(runbook_tables)) == "0010_runbook_engine"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("downgrade", "0009_state_prediction")
    assert asyncio.run(revision(detection_tables)) == "0009_state_prediction"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("check")
    migrate("downgrade", "0008_scheduled_events")
    assert asyncio.run(revision(event_tables)) == "0008_scheduled_events"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("check")
    migrate("downgrade", "0007_ops_events")
    assert asyncio.run(revision(event_tables)) == "0007_ops_events"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("check")
    migrate("downgrade", "0006_knowledge_brain")
    assert asyncio.run(revision(knowledge_tables)) == "0006_knowledge_brain"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("check")
    migrate("downgrade", "0005_change_timeline")
    assert asyncio.run(revision(timeline_tables)) == "0005_change_timeline"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("check")
    migrate("downgrade", "0004_context_graph")
    assert asyncio.run(revision(graph_tables)) == "0004_context_graph"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("check")
    migrate("downgrade", "0003_evidence_ledger")
    assert asyncio.run(revision(ledger_tables)) == "0003_evidence_ledger"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("downgrade", "0002_ai_tasks")
    assert asyncio.run(revision(task_tables)) == "0002_ai_tasks"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("downgrade", "0001_database_foundation")
    assert asyncio.run(revision(["alembic_version"])) == "0001_database_foundation"
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
    migrate("downgrade", "base")
    assert asyncio.run(revision(["alembic_version"])) is None
    migrate("upgrade", "head")
    assert asyncio.run(revision(head_tables)) == "0016_catalog_audit"
