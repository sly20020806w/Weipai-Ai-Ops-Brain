"""在验收入口创建的独立临时库演示三条规则的检索、更新、删除。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.agent.fake import EmbeddingStep, FakeLLM
from app.agent.models import EmbeddingRequest, EmbeddingResponse
from app.db.session import Database
from app.knowledge.models import KnowledgeEntry
from app.knowledge.schemas import KnowledgeDraft, KnowledgeSearch, KnowledgeType
from app.knowledge.service import KnowledgeNotFound, KnowledgeService


async def run_demo(database: Database) -> None:
    at = datetime(2026, 10, 6, tzinfo=UTC)
    texts = (
        "支付高峰期禁止停机",
        "数据库慢查询必须先收集执行计划",
        "资源闲置满七天后评估回收",
    )
    question = "支付高峰期是否允许停机？"
    updated_text = "数据库连接异常时先检查连接池"
    model = f"fake-demo-{uuid4().hex}"

    def step(content: str, vector: tuple[float, ...]) -> EmbeddingStep:
        return EmbeddingStep(
            request=EmbeddingRequest(inputs=(content,)),
            response=EmbeddingResponse(model=model, vectors=(vector,)),
        )

    def draft(content: str) -> KnowledgeDraft:
        return KnowledgeDraft(
            kind=KnowledgeType.BUSINESS_RULE,
            content=content,
            source="业务负责人说明（Fake 验收样例）",
            valid_from=at,
        )

    llm = FakeLLM(
        [
            step(texts[0], (1.0, 0.0, 0.0)),
            step(texts[1], (0.0, 1.0, 0.0)),
            step(texts[2], (0.0, 0.0, 1.0)),
            step(question, (1.0, 0.0, 0.0)),
            step(updated_text, (0.0, 1.0, 0.0)),
            step(question, (1.0, 0.0, 0.0)),
            step(question, (0.0, 1.0, 0.0)),
        ]
    )
    try:
        async with database.session() as session, session.begin():
            service = KnowledgeService(session, llm)
            entries = [await service.create(draft(content)) for content in texts]
        async with database.session() as session:
            service = KnowledgeService(session, llm)
            matches = await service.search(KnowledgeSearch(query=question, at=at))
            assert len(matches) == 3 and matches[0].entry.id == entries[0].id
            print(f"已写入 3 条业务规则；查询：{question}", flush=True)
            for match in matches:
                print(
                    f"相似度 {match.similarity:.3f}：{match.entry.content}（{match.entry.id}）",
                    flush=True,
                )
        async with database.session() as session, session.begin():
            await KnowledgeService(session, llm).update(entries[0].id, draft(updated_text))
        async with database.session() as session:
            entry = await session.scalar(
                select(KnowledgeEntry).where(KnowledgeEntry.id == entries[0].id)
            )
            assert entry is not None and list(entry.embedding) == [0.0, 1.0, 0.0]
            hits = await KnowledgeService(session, llm).search(
                KnowledgeSearch(query=question, at=at)
            )
            changed = next(hit for hit in hits if hit.entry.id == entries[0].id)
            assert changed.similarity == 0.0
            print(
                f"更新后：{changed.entry.content}，向量 [0, 1, 0]，原查询相似度 0.000", flush=True
            )
        async with database.session() as session, session.begin():
            await KnowledgeService(session, llm).delete(entries[0].id)
        async with database.session() as session:
            service = KnowledgeService(session, llm)
            hits = await service.search(KnowledgeSearch(query=question, at=at))
            assert len(hits) == 2 and entries[0].id not in {hit.entry.id for hit in hits}
            try:
                await service.get(entries[0].id)
            except KnowledgeNotFound:
                pass
            else:
                raise AssertionError("删除后仍可读回知识")
            print("删除后：原条目不可读回或检索，剩余 2 条规则", flush=True)
        assert llm.remaining_steps == 0
        print("Step 20 Fake 知识演示通过（实际 pgvector 检索）", flush=True)
    finally:
        await llm.aclose()
