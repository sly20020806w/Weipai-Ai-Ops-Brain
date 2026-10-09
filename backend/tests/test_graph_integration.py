"""临时本地 PostgreSQL：图约束、原子 upsert、UTC 与 N 跳查询。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.config import parse_database_url
from app.db.session import Database
from app.graph.models import GraphEdge, GraphNode
from app.graph.service import GraphNodeNotFound, GraphService
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


async def node_pair(database: Database) -> tuple[UUID, UUID]:
    suffix = uuid4().hex
    async with database.session() as session, session.begin():
        service = GraphService(session)
        source = await service.upsert_node(
            kind="service", external_id=f"fake://payment/{suffix}", name="payment-service"
        )
        target = await service.upsert_node(
            kind="database", external_id=f"fake://db/{suffix}", name="payment-db"
        )
        return source.id, target.id


@pytest.mark.asyncio
async def test_node_identity_and_name_refresh_cross_session(database: Database) -> None:
    key = f"fake://payment/{uuid4().hex}"
    async with database.session() as session, session.begin():
        service = GraphService(session)
        first = await service.upsert_node(kind="service", external_id=key, name="支付")
        node_id, created_at = first.id, first.created_at
        repeated = await service.upsert_node(kind="service", external_id=key, name="支付服务")
        assert repeated.id == node_id and repeated.name == "支付服务"
        other = await service.upsert_node(kind="repository", external_id=key, name="支付仓库")
        assert other.id != node_id
    async with database.session() as session:
        node = await GraphService(session).get_node(node_id)
        assert node.name == "支付服务" and node.created_at == created_at
        assert node.created_at.tzinfo is UTC and node.updated_at.tzinfo is UTC
        assert (
            await session.scalar(
                select(func.count()).select_from(GraphNode).where(GraphNode.external_id == key)
            )
            == 2
        )


@pytest.mark.asyncio
async def test_edge_upsert_only_refreshes_last_seen_and_preserves_other_sources(
    database: Database,
) -> None:
    source, target = await node_pair(database)
    start = datetime(2026, 10, 6, 8, tzinfo=timezone(timedelta(hours=8)))
    async with database.session() as session, session.begin():
        service = GraphService(session)
        first = await service.upsert_edge(
            from_node_id=source,
            to_node_id=target,
            relation="uses",
            source="fake.cmdb",
            confidence=0.8,
            observed_at=start,
        )
        edge_id, created_at, updated_at = first.id, first.created_at, first.updated_at
        repeated = await service.upsert_edge(
            from_node_id=source,
            to_node_id=target,
            relation="uses",
            source="fake.cmdb",
            confidence=0.1,
            observed_at=start + timedelta(minutes=10),
        )
        assert repeated.id == edge_id and repeated.first_seen == start
        assert repeated.last_seen == start + timedelta(minutes=10)
        assert repeated.confidence == 0.8
        assert repeated.created_at == created_at and repeated.updated_at == updated_at
        stale = await service.upsert_edge(
            from_node_id=source,
            to_node_id=target,
            relation="uses",
            source="fake.cmdb",
            confidence=1,
            observed_at=start - timedelta(minutes=5),
        )
        assert stale.first_seen == start and stale.last_seen == start + timedelta(minutes=10)
        other_source = await service.upsert_edge(
            from_node_id=source,
            to_node_id=target,
            relation="uses",
            source="fake.arms",
            confidence=1,
        )
        other_relation = await service.upsert_edge(
            from_node_id=source,
            to_node_id=target,
            relation="owns",
            source="fake.cmdb",
            confidence=1,
        )
        assert len({edge_id, other_source.id, other_relation.id}) == 3
    async with database.session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(GraphEdge).where(GraphEdge.from_node_id == source)
            )
            == 3
        )
        edge = await session.get(GraphEdge, edge_id)
        assert edge is not None
        assert edge.first_seen.tzinfo is UTC and edge.last_seen.tzinfo is UTC
        assert edge.freshness_at(start + timedelta(minutes=12)) == timedelta(minutes=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "missing_source",
        "missing_confidence",
        "blank_source",
        "blank_relation",
        "negative",
        "above_one",
        "nan",
        "infinity",
        "missing_endpoint",
        "time_order",
    ],
)
async def test_database_rejects_invalid_edges_without_service(
    database: Database, invalid: str
) -> None:
    source, target = await node_pair(database)
    now = datetime(2026, 10, 6, tzinfo=UTC)
    values: dict[str, object] = dict(
        from_node_id=source,
        to_node_id=target,
        relation="uses",
        source="fake.cmdb",
        confidence=0.8,
        first_seen=now,
        last_seen=now,
    )
    changes: dict[str, dict[str, object]] = {
        "missing_source": {"source": None},
        "missing_confidence": {"confidence": None},
        "blank_source": {"source": " "},
        "blank_relation": {"relation": " "},
        "negative": {"confidence": -0.1},
        "above_one": {"confidence": 1.1},
        "nan": {"confidence": float("nan")},
        "infinity": {"confidence": float("inf")},
        "missing_endpoint": {"to_node_id": uuid4()},
        "time_order": {"last_seen": now - timedelta(seconds=1)},
    }
    values.update(changes[invalid])
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            await session.execute(insert(GraphEdge).values(**values))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["source", "confidence", "first_seen", "last_seen"])
async def test_database_requires_edge_observation_fields(database: Database, field: str) -> None:
    source, target = await node_pair(database)
    values: dict[str, object] = dict(
        from_node_id=source,
        to_node_id=target,
        relation="uses",
        source="fake.cmdb",
        confidence=0.8,
        first_seen=datetime(2026, 10, 6, tzinfo=UTC),
        last_seen=datetime(2026, 10, 6, tzinfo=UTC),
    )
    del values[field]
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            await session.execute(insert(GraphEdge).values(**values))


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [0, 1])
async def test_confidence_boundaries_are_valid(database: Database, confidence: float) -> None:
    source, target = await node_pair(database)
    async with database.session() as session, session.begin():
        edge = await GraphService(session).upsert_edge(
            from_node_id=source,
            to_node_id=target,
            relation="uses",
            source="fake.cmdb",
            confidence=confidence,
        )
        assert edge.confidence == confidence


@pytest.mark.asyncio
async def test_two_hops_directions_cycles_diamonds_and_isolated_node(database: Database) -> None:
    suffix = uuid4().hex
    nodes: dict[str, UUID] = {}
    async with database.session() as session, session.begin():
        service = GraphService(session)
        for name in [
            "payment-service",
            "payment-db",
            "payment-cache",
            "db-host",
            "zone",
            "caller",
            "isolated",
        ]:
            node = await service.upsert_node(
                kind="resource", external_id=f"{suffix}/{name}", name=name
            )
            nodes[name] = node.id
        for start, end in [
            ("payment-service", "payment-db"),
            ("payment-service", "payment-cache"),
            ("payment-db", "db-host"),
            ("payment-cache", "db-host"),
            ("db-host", "zone"),
            ("payment-db", "payment-service"),
            ("caller", "payment-service"),
            ("payment-service", "payment-service"),
        ]:
            await service.upsert_edge(
                from_node_id=nodes[start],
                to_node_id=nodes[end],
                relation="uses",
                source="fake.cmdb",
                confidence=1,
            )
        # 同一关系的第二个来源不应使邻居重复。
        await service.upsert_edge(
            from_node_id=nodes["payment-service"],
            to_node_id=nodes["payment-db"],
            relation="uses",
            source="fake.arms",
            confidence=0.9,
        )
    async with database.session() as session:
        service = GraphService(session)
        root = nodes["payment-service"]
        assert await service.neighbors(root, hops=0) == []
        assert {n.name for n in await service.neighbors(root, hops=1)} == {
            "payment-db",
            "payment-cache",
        }
        result = await service.neighbors(root, hops=2)
        assert {n.name for n in result} == {"payment-db", "payment-cache", "db-host"}
        assert len(result) == 3
        assert [n.external_id for n in result] == sorted(n.external_id for n in result)
        assert {n.name for n in await service.neighbors(root, hops=3)} == {
            "payment-db",
            "payment-cache",
            "db-host",
            "zone",
        }
        assert {n.name for n in await service.neighbors(root, hops=1, direction="incoming")} == {
            "payment-db",
            "caller",
        }
        assert {n.name for n in await service.neighbors(root, hops=2, direction="both")} == {
            "payment-db",
            "payment-cache",
            "db-host",
            "caller",
        }
        assert await service.neighbors(nodes["isolated"], hops=10) == []
        with pytest.raises(GraphNodeNotFound):
            await service.neighbors(uuid4(), hops=2)


@pytest.mark.asyncio
async def test_concurrent_edge_upserts_are_unique_and_monotonic(database: Database) -> None:
    source, target = await node_pair(database)
    start = datetime(2026, 10, 6, tzinfo=UTC)

    async def observe(minute: int) -> UUID:
        async with database.session() as session, session.begin():
            edge = await GraphService(session).upsert_edge(
                from_node_id=source,
                to_node_id=target,
                relation="uses",
                source="fake.cmdb",
                confidence=0.8,
                observed_at=start + timedelta(minutes=minute),
            )
            return edge.id

    ids = await asyncio.gather(*(observe(minute) for minute in [0, 4, 1, 3, 2]))
    assert len(set(ids)) == 1
    async with database.session() as session:
        edge = await session.get(GraphEdge, ids[0])
        assert edge is not None and edge.last_seen == start + timedelta(minutes=4)
        assert (
            await session.scalar(
                select(func.count()).select_from(GraphEdge).where(GraphEdge.from_node_id == source)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_node_and_edge_transaction_rolls_back_together(database: Database) -> None:
    source, target = await node_pair(database)
    key = uuid4().hex
    with pytest.raises(RuntimeError, match="模拟失败"):
        async with database.session() as session, session.begin():
            service = GraphService(session)
            node = await service.upsert_node(kind="service", external_id=key, name="临时节点")
            await service.upsert_edge(
                from_node_id=node.id,
                to_node_id=target,
                relation="uses",
                source="fake.cmdb",
                confidence=1,
            )
            await service.upsert_edge(
                from_node_id=source,
                to_node_id=target,
                relation="uses",
                source="fake.cmdb",
                confidence=1,
            )
            raise RuntimeError("模拟失败")
    async with database.session() as session:
        assert await session.scalar(select(GraphNode).where(GraphNode.external_id == key)) is None
        assert (
            await session.scalar(select(GraphEdge).where(GraphEdge.from_node_id == source)) is None
        )


@pytest.mark.asyncio
async def test_direct_orm_naive_timestamp_rejected(database: Database) -> None:
    source, target = await node_pair(database)
    with pytest.raises(StatementError, match="带时区"):
        async with database.session() as session, session.begin():
            session.add(
                GraphEdge(
                    from_node_id=source,
                    to_node_id=target,
                    relation="uses",
                    source="fake.cmdb",
                    confidence=1,
                    first_seen=datetime(2026, 10, 6),
                    last_seen=datetime(2026, 10, 6),
                )
            )


@pytest.mark.asyncio
async def test_graph_schema_uses_uuid_utc_and_no_freshness_column(database: Database) -> None:
    async with database.engine.connect() as connection:
        columns = (
            await connection.execute(
                text(
                    "SELECT table_name, column_name, data_type, is_nullable "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name IN ('context_graph_nodes', 'context_graph_edges')"
                )
            )
        ).all()
        types = {(row[0], row[1]): (row[2], row[3]) for row in columns}
        for table in ["context_graph_nodes", "context_graph_edges"]:
            assert types[table, "id"] == ("uuid", "NO")
            for name in ["created_at", "updated_at"]:
                assert types[table, name] == ("timestamp with time zone", "NO")
        for name in ["first_seen", "last_seen"]:
            assert types["context_graph_edges", name] == ("timestamp with time zone", "NO")
        assert not any(column == "freshness" for _, column in types)
