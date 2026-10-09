"""Step 20 使用真实本地 pgvector 和 Fake embeddings；不触达生产系统。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.agent.client import GatewayTimeout
from app.agent.fake import EmbeddingStep, FakeLLM
from app.agent.models import EmbeddingRequest
from app.config import parse_database_url
from app.db.session import Database
from app.knowledge.models import KnowledgeEntry
from app.knowledge.schemas import KnowledgePage, KnowledgeSearch, KnowledgeType
from app.knowledge.service import KnowledgeNotFound, KnowledgeService
from tests.database_support import get_test_database_url, migrate
from tests.test_knowledge import AT, draft, step

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="执行 check-knowledge.ps1 进行本地 pgvector 验收",
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


@pytest.mark.asyncio
async def test_crud_ranking_update_vector_and_delete_across_sessions(database: Database) -> None:
    # 独立模型空间避免被总回归的其他知识数据干扰。
    model = f"fake-ranking-{uuid4().hex}"
    rules = [
        draft("支付高峰期禁止停机"),
        draft("数据库慢查询必须先收集执行计划"),
        draft("资源闲置满七天后评估回收"),
    ]
    question = "支付业务高峰期可以停机吗？"
    changed = draft("数据库连接异常时先检查连接池")
    llm = FakeLLM(
        [
            step(rules[0].content, (1.0, 0.0, 0.0), model),
            step(rules[1].content, (0.0, 1.0, 0.0), model),
            step(rules[2].content, (0.0, 0.0, 1.0), model),
            step(question, (1.0, 0.0, 0.0), model),
            step(changed.content, (0.0, 1.0, 0.0), model),
            step(question, (1.0, 0.0, 0.0), model),
            step(question, (0.0, 1.0, 0.0), model),
        ]
    )
    async with database.session() as session, session.begin():
        service = KnowledgeService(session, llm)
        entries = [await service.create(rule) for rule in rules]
    async with database.session() as session:
        service = KnowledgeService(session, llm)
        hits = await service.search(KnowledgeSearch(query=question, at=AT, limit=3))
        assert {hit.entry.id for hit in hits} == {entry.id for entry in entries}
        assert hits[0].entry.id == entries[0].id and hits[0].similarity == pytest.approx(1.0)
        assert await service.get(entries[0].id) == entries[0]
        assert all(e.created_at.tzinfo is UTC and e.updated_at.tzinfo is UTC for e in entries)
    async with database.session() as session, session.begin():
        updated = await KnowledgeService(session, llm).update(entries[0].id, changed)
        assert updated.id == entries[0].id and updated.created_at == entries[0].created_at
        assert updated.updated_at >= entries[0].updated_at
    async with database.session() as session:
        loaded = await session.get(KnowledgeEntry, entries[0].id)
        assert loaded is not None and list(loaded.embedding) == [0.0, 1.0, 0.0]
        hits = await KnowledgeService(session, llm).search(KnowledgeSearch(query=question, at=AT))
        assert next(h.similarity for h in hits if h.entry.id == entries[0].id) == pytest.approx(0.0)
    async with database.session() as session, session.begin():
        await KnowledgeService(session, llm).delete(entries[0].id)
    async with database.session() as session:
        service = KnowledgeService(session, llm)
        with pytest.raises(KnowledgeNotFound):
            await service.get(entries[0].id)
        assert entries[0].id not in {
            h.entry.id for h in await service.search(KnowledgeSearch(query=question, at=AT))
        }
    assert llm.remaining_steps == 0 and len(llm.calls) == 7


@pytest.mark.asyncio
async def test_effective_window_model_dimension_and_type_filters(database: Database) -> None:
    model = f"fake-spaces-{uuid4().hex}"
    llm = FakeLLM(
        [
            step("已生效", (1.0, 0.0), model),
            step("已过期", (1.0, 0.0), model),
            step("未生效", (1.0, 0.0), model),
            step("其他模型", (1.0, 0.0), model + "-other"),
            step("其他维度", (1.0, 0.0, 0.0), model),
            step("操作规范", (0.0, 1.0), model),
            *[step("查询", (1.0, 0.0), model) for _ in range(3)],
        ]
    )
    async with database.session() as session, session.begin():
        service = KnowledgeService(session, llm)
        valid = await service.create(draft("已生效", expires_at=AT + timedelta(seconds=1)))
        await service.create(draft("已过期", valid_from=AT - timedelta(days=1), expires_at=AT))
        await service.create(draft("未生效", valid_from=AT + timedelta(seconds=1)))
        await service.create(draft("其他模型"))
        await service.create(draft("其他维度"))
        standard = await service.create(draft("操作规范", kind=KnowledgeType.STANDARD))
    async with database.session() as session:
        service = KnowledgeService(session, llm)
        hits = await service.search(KnowledgeSearch(query="查询", at=AT))
        assert [h.entry.id for h in hits] == [valid.id, standard.id]
        hits = await service.search(
            KnowledgeSearch(query="查询", at=AT, kind=KnowledgeType.STANDARD, limit=1)
        )
        assert [h.entry.id for h in hits] == [standard.id]
        hits = await service.search(KnowledgeSearch(query="查询", at=AT + timedelta(seconds=1)))
        assert valid.id not in {h.entry.id for h in hits}


@pytest.mark.asyncio
async def test_rollback_and_embedding_failure_keep_old_content_vector(database: Database) -> None:
    model = f"fake-atomic-{uuid4().hex}"
    llm = FakeLLM(
        [
            step("原规则", (1.0, 0.0), model),
            step("事务回滚", (0.0, 1.0), model),
            EmbeddingStep(
                request=EmbeddingRequest(inputs=("生成失败",)),
                response=GatewayTimeout("模拟网关失败"),
            ),
            step("新建回滚", (0.0, 1.0), model),
        ]
    )
    async with database.session() as session, session.begin():
        original = await KnowledgeService(session, llm).create(draft("原规则"))
    with pytest.raises(RuntimeError, match="回滚"):
        async with database.session() as session, session.begin():
            await KnowledgeService(session, llm).update(original.id, draft("事务回滚"))
            raise RuntimeError("回滚")
    async with database.session() as session, session.begin():
        service = KnowledgeService(session, llm)
        with pytest.raises(GatewayTimeout):
            await service.update(original.id, draft("生成失败"))
        assert await service.get(original.id) == original
        loaded = await session.get(KnowledgeEntry, original.id)
        assert loaded is not None and list(loaded.embedding) == [1.0, 0.0]
    with pytest.raises(RuntimeError, match="回滚"):
        async with database.session() as session, session.begin():
            rolled_back = await KnowledgeService(session, llm).create(draft("新建回滚"))
            raise RuntimeError("回滚")
    async with database.session() as session:
        with pytest.raises(KnowledgeNotFound):
            await KnowledgeService(session, llm).get(rolled_back.id)


@pytest.mark.asyncio
async def test_missing_entries_and_list_pagination(database: Database) -> None:
    llm = FakeLLM([step("团队约定一", (1.0,)), step("团队约定二", (1.0,))])
    async with database.session() as session, session.begin():
        service = KnowledgeService(session, llm)
        for operation in (
            service.get(uuid4()),
            service.update(uuid4(), draft()),
            service.delete(uuid4()),
        ):
            with pytest.raises(KnowledgeNotFound):
                await operation
        assert not llm.calls
        first = await service.create(draft("团队约定一", kind=KnowledgeType.TEAM_CONVENTION))
        second = await service.create(draft("团队约定二", kind=KnowledgeType.TEAM_CONVENTION))
        all_entries = await service.list(KnowledgePage(kind=KnowledgeType.TEAM_CONVENTION))
        one = await service.list(KnowledgePage(kind=KnowledgeType.TEAM_CONVENTION, limit=1))
        two = await service.list(
            KnowledgePage(kind=KnowledgeType.TEAM_CONVENTION, limit=1, offset=1)
        )
        assert {e.id for e in all_entries} == {first.id, second.id}
        assert (*one, *two) == all_entries


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "machine_fact"},
        {"content": " "},
        {"source": ""},
        {"embedding_model": " "},
        {"embedding_dimensions": 3},
        {"embedding": [0.0, 0.0]},
        {"expires_at": AT},
    ],
)
@pytest.mark.asyncio
async def test_database_constraints_reject_invalid_records(
    database: Database, changes: dict[str, object]
) -> None:
    values: dict[str, object] = {
        "kind": "business_rule",
        "content": "业务规则",
        "source": "业务方",
        "valid_from": AT,
        "embedding_model": "fake",
        "embedding_dimensions": 2,
        "embedding": [1.0, 0.0],
    }
    with pytest.raises(IntegrityError):
        async with database.session() as session, session.begin():
            session.add(KnowledgeEntry(**(values | changes)))
            await session.flush()


@pytest.mark.asyncio
async def test_actual_vector_column_extension_and_timezone(database: Database) -> None:
    async with database.session() as session:
        assert await session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        )
        rows = await session.execute(
            text(
                "SELECT column_name, udt_name FROM information_schema.columns "
                "WHERE table_name='knowledge_entries'"
            )
        )
        types = {str(name): str(kind) for name, kind in rows.tuples()}
        assert types["embedding"] == "vector"
        assert all(
            types[c] == "timestamptz"
            for c in ("created_at", "updated_at", "valid_from", "expires_at")
        )


@pytest.mark.asyncio
async def test_concurrent_updates_keep_content_and_embedding_together(database: Database) -> None:
    async with database.session() as session, session.begin():
        original = await KnowledgeService(session, FakeLLM([step("原始", (1.0, 0.0))])).create(
            draft("原始")
        )

    async def change(content: str, vector: tuple[float, ...]) -> None:
        async with database.session() as session, session.begin():
            await KnowledgeService(session, FakeLLM([step(content, vector)])).update(
                original.id, draft(content)
            )

    await asyncio.gather(change("更新甲", (1.0, 0.0)), change("更新乙", (0.0, 1.0)))
    async with database.session() as session:
        loaded = await session.scalar(
            select(KnowledgeEntry).where(KnowledgeEntry.id == original.id)
        )
        assert loaded is not None
        expected = {"更新甲": [1.0, 0.0], "更新乙": [0.0, 1.0]}
        assert list(loaded.embedding) == expected[loaded.content]


@pytest.mark.parametrize("scale", [1e-30, 1e30])
@pytest.mark.asyncio
async def test_extreme_vectors_have_finite_database_cosine_scores(
    database: Database, scale: float
) -> None:
    model = f"fake-extreme-{uuid4().hex}"
    llm = FakeLLM(
        [step("极值规则", (scale, scale), model), step("极值查询", (scale, scale), model)]
    )
    async with database.session() as session, session.begin():
        entry = await KnowledgeService(session, llm).create(draft("极值规则"))
    async with database.session() as session:
        hits = await KnowledgeService(session, llm).search(KnowledgeSearch(query="极值查询", at=AT))
        assert len(hits) == 1 and hits[0].entry.id == entry.id
        assert hits[0].similarity == pytest.approx(1.0)
