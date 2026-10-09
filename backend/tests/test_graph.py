"""图服务入参及 freshness 契约；普通检查不连接外部系统。"""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.models import GraphEdge
from app.graph.service import GraphNodeNotFound, GraphService


@pytest.fixture
def session() -> AsyncMock:
    instance = AsyncMock(spec=AsyncSession)
    instance.in_transaction.return_value = True
    return instance


def test_freshness_uses_last_seen_and_normalizes_utc() -> None:
    edge = GraphEdge(
        first_seen=datetime(2026, 10, 5, tzinfo=UTC),
        last_seen=datetime(2026, 10, 6, 8, tzinfo=timezone(timedelta(hours=8))),
    )
    assert edge.freshness_at(datetime(2026, 10, 6, 0, 5, tzinfo=UTC)) == timedelta(minutes=5)
    assert edge.freshness_at(datetime(2026, 10, 5, tzinfo=UTC)) == timedelta(0)
    assert isinstance(edge.freshness, timedelta)
    with pytest.raises(ValueError, match="带时区"):
        edge.freshness_at(datetime(2026, 10, 6))
    assert "freshness" not in GraphEdge.__table__.columns


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["kind", "external_id", "name"])
@pytest.mark.parametrize("value", ["", " \n", None, "x" * 301])
async def test_invalid_nodes_fail_before_write(
    session: AsyncMock, field: str, value: object
) -> None:
    arguments: dict[str, object] = dict(kind="service", external_id="payment", name="支付服务")
    arguments[field] = value
    with pytest.raises(ValueError):
        await GraphService(session).upsert_node(**arguments)  # type: ignore[arg-type]
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"source": None},
        {"source": " \n"},
        {"source": "x" * 201},
        {"relation": " "},
        {"relation": "x" * 101},
        {"confidence": None},
        {"confidence": True},
        {"confidence": "0.8"},
        {"confidence": -0.01},
        {"confidence": 1.01},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"observed_at": datetime(2026, 10, 6)},
    ],
)
async def test_invalid_edges_fail_before_write(
    session: AsyncMock, changes: dict[str, object]
) -> None:
    arguments = dict(
        from_node_id=uuid4(),
        to_node_id=uuid4(),
        relation="uses",
        source="fake.cmdb",
        confidence=0.8,
    )
    arguments.update(changes)
    with pytest.raises(ValueError):
        await GraphService(session).upsert_edge(**arguments)  # type: ignore[arg-type]
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["source", "confidence"])
async def test_edge_source_and_confidence_are_required(session: AsyncMock, field: str) -> None:
    arguments = dict(
        from_node_id=uuid4(),
        to_node_id=uuid4(),
        relation="uses",
        source="fake.cmdb",
        confidence=0.8,
    )
    del arguments[field]
    with pytest.raises(TypeError, match=field):
        await GraphService(session).upsert_edge(**arguments)  # type: ignore[arg-type]
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_writes_require_explicit_transaction(session: AsyncMock) -> None:
    session.in_transaction.return_value = False
    service = GraphService(session)
    with pytest.raises(RuntimeError, match="开启事务"):
        await service.upsert_node(kind="service", external_id="payment", name="支付")
    with pytest.raises(RuntimeError, match="开启事务"):
        await service.upsert_edge(
            from_node_id=uuid4(),
            to_node_id=uuid4(),
            relation="uses",
            source="fake.cmdb",
            confidence=1,
        )
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hops", [-1, True, 1.5, "2"])
async def test_invalid_hops_are_rejected(session: AsyncMock, hops: object) -> None:
    with pytest.raises(ValueError, match="hops"):
        await GraphService(session).neighbors(uuid4(), hops=hops)  # type: ignore[arg-type]
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_direction_and_missing_root(session: AsyncMock) -> None:
    service = GraphService(session)
    with pytest.raises(ValueError, match="direction"):
        await service.neighbors(uuid4(), hops=2, direction="invalid")  # type: ignore[arg-type]
    session.get.return_value = None
    with pytest.raises(GraphNodeNotFound):
        await service.neighbors(uuid4(), hops=2)
