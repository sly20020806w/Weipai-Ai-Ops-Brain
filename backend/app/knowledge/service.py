"""知识 CRUD 与数据库内精确余弦检索；事务和 LLM 生命周期由调用方管理。"""

import math
import struct
from typing import cast
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.agent.client import LLMClient
from app.agent.models import EmbeddingRequest, EmbeddingResponse
from app.knowledge.models import KnowledgeEntry
from app.knowledge.schemas import (
    KnowledgeDraft,
    KnowledgeMatch,
    KnowledgePage,
    KnowledgeSearch,
    KnowledgeType,
    KnowledgeView,
)


class KnowledgeNotFound(LookupError):
    pass


class KnowledgeEmbeddingError(ValueError):
    pass


def validate_embedding(response: EmbeddingResponse) -> tuple[str, list[float]]:
    """校验 float32 后单位化，避免余弦点积在极大/极小向量上溢出。"""
    response = EmbeddingResponse.model_validate(response)
    if len(response.vectors) != 1 or not 1 <= len(response.vectors[0]) <= 16000:
        raise KnowledgeEmbeddingError("知识 embedding 必须返回一条 1–16000 维向量")
    if not response.model.strip() or len(response.model) > 256:
        raise KnowledgeEmbeddingError("知识 embedding 模型标识无效")
    try:
        vector = [float(struct.unpack("f", struct.pack("f", v))[0]) for v in response.vectors[0]]
    except (OverflowError, struct.error):
        raise KnowledgeEmbeddingError("知识 embedding 超出 float32 范围") from None
    if not all(math.isfinite(v) for v in vector) or not any(v != 0 for v in vector):
        raise KnowledgeEmbeddingError("知识 embedding 必须为有限的非零向量")
    norm = math.hypot(*vector)
    normalized = [float(struct.unpack("f", struct.pack("f", v / norm))[0]) for v in vector]
    return response.model, normalized


def view(entry: KnowledgeEntry) -> KnowledgeView:
    return KnowledgeView(
        id=entry.id,
        kind=KnowledgeType(entry.kind),
        content=entry.content,
        source=entry.source,
        valid_from=entry.valid_from,
        expires_at=entry.expires_at,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        embedding_model=entry.embedding_model,
        embedding_dimensions=entry.embedding_dimensions,
    )


class KnowledgeService:
    def __init__(self, session: AsyncSession, llm: LLMClient) -> None:
        self.session = session
        self.llm = llm

    async def _embedding(self, content: str) -> tuple[str, list[float]]:
        return validate_embedding(await self.llm.embeddings(EmbeddingRequest(inputs=(content,))))

    async def create(self, draft: KnowledgeDraft) -> KnowledgeView:
        draft = KnowledgeDraft.model_validate(draft)
        model, vector = await self._embedding(draft.content)
        entry = KnowledgeEntry(
            **draft.model_dump(),
            embedding=vector,
            embedding_model=model,
            embedding_dimensions=len(vector),
        )
        self.session.add(entry)
        await self.session.flush()
        return view(entry)

    async def get(self, entry_id: UUID) -> KnowledgeView:
        entry = await self.session.get(KnowledgeEntry, entry_id, populate_existing=True)
        if entry is None:
            raise KnowledgeNotFound("知识条目不存在")
        return view(entry)

    async def list(self, page: KnowledgePage | None = None) -> tuple[KnowledgeView, ...]:
        page = KnowledgePage.model_validate(page or KnowledgePage())
        statement = select(KnowledgeEntry)
        if page.kind is not None:
            statement = statement.where(KnowledgeEntry.kind == page.kind.value)
        entries = await self.session.scalars(
            statement.order_by(KnowledgeEntry.created_at.desc(), KnowledgeEntry.id)
            .offset(page.offset)
            .limit(page.limit)
        )
        return tuple(view(entry) for entry in entries)

    async def update(self, entry_id: UUID, draft: KnowledgeDraft) -> KnowledgeView:
        draft = KnowledgeDraft.model_validate(draft)
        await self.get(entry_id)
        # 先生成并验证向量；任何 LLM 错误都不修改原条目。
        model, vector = await self._embedding(draft.content)
        entry = await self.session.scalar(
            update(KnowledgeEntry)
            .where(KnowledgeEntry.id == entry_id)
            .values(
                **draft.model_dump(),
                embedding=vector,
                embedding_model=model,
                embedding_dimensions=len(vector),
            )
            .returning(KnowledgeEntry)
            .execution_options(populate_existing=True)
        )
        if entry is None:
            raise KnowledgeNotFound("知识条目不存在")
        return view(entry)

    async def delete(self, entry_id: UUID) -> None:
        deleted = await self.session.scalar(
            delete(KnowledgeEntry).where(KnowledgeEntry.id == entry_id).returning(KnowledgeEntry.id)
        )
        if deleted is None:
            raise KnowledgeNotFound("知识条目不存在")

    async def search(self, query: KnowledgeSearch) -> tuple[KnowledgeMatch, ...]:
        query = KnowledgeSearch.model_validate(query)
        model, vector = await self._embedding(query.query)
        distance = cast(ColumnElement[float], KnowledgeEntry.embedding.cosine_distance(vector))
        statement = select(KnowledgeEntry, distance).where(
            KnowledgeEntry.embedding_model == model,
            KnowledgeEntry.embedding_dimensions == len(vector),
            KnowledgeEntry.valid_from <= query.at,
            or_(KnowledgeEntry.expires_at.is_(None), KnowledgeEntry.expires_at > query.at),
        )
        if query.kind is not None:
            statement = statement.where(KnowledgeEntry.kind == query.kind.value)
        rows = await self.session.execute(
            statement.order_by(distance, KnowledgeEntry.id).limit(query.limit)
        )
        matches: list[KnowledgeMatch] = []
        for entry, value in rows.tuples():
            if not math.isfinite(value):
                raise KnowledgeEmbeddingError("知识检索返回了无效的余弦距离")
            matches.append(
                KnowledgeMatch(entry=view(entry), similarity=max(-1.0, min(1.0, 1.0 - value)))
            )
        return tuple(matches)
