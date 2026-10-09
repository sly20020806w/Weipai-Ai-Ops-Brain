"""完整内容和向量原子保存；事务由调用方管理，检索不代表适用。"""

import json
import math
from collections.abc import Callable
from typing import cast
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.agent.client import LLMClient
from app.agent.models import EmbeddingRequest
from app.knowledge.service import validate_embedding
from app.runbooks.models import Runbook
from app.runbooks.schemas import (
    AutomationLevel,
    RunbookDraft,
    RunbookHit,
    RunbookMaturity,
    RunbookSearch,
    RunbookView,
)


class RunbookNotFound(LookupError):
    pass


def view(entry: Runbook) -> RunbookView:
    data = {name: getattr(entry, name) for name in RunbookDraft.model_fields}
    data.update(
        id=str(entry.id),
        created_at=entry.created_at.isoformat(),
        updated_at=entry.updated_at.isoformat(),
        embedding_model=entry.embedding_model,
        embedding_dimensions=entry.embedding_dimensions,
        content_version=entry.content_version,
    )
    return RunbookView.model_validate_json(json.dumps(data))


class RunbookService:
    def __init__(
        self, session: AsyncSession, llm_factory: Callable[[EmbeddingRequest], LLMClient]
    ) -> None:
        self.session, self.llm_factory = session, llm_factory

    async def _embedding(self, text: str) -> tuple[str, list[float]]:
        request = EmbeddingRequest(inputs=(text,))
        llm = self.llm_factory(request)
        try:
            return validate_embedding(await llm.embeddings(request))
        finally:
            await llm.aclose()

    async def _values(self, draft: RunbookDraft) -> dict[str, object]:
        draft = RunbookDraft.model_validate(draft)
        # 条件或步骤改变也更新向量，不能保留旧正文对应的 embedding。
        model, vector = await self._embedding(draft.model_dump_json())
        return dict(
            draft.model_dump(mode="json"),
            embedding=vector,
            embedding_model=model,
            embedding_dimensions=len(vector),
        )

    async def create(self, draft: RunbookDraft) -> RunbookView:
        entry = Runbook(**await self._values(draft))
        self.session.add(entry)
        await self.session.flush()
        return view(entry)

    async def get(self, runbook_id: UUID) -> RunbookView:
        entry = await self.session.get(Runbook, runbook_id, populate_existing=True)
        if entry is None:
            raise RunbookNotFound("Runbook 不存在")
        return view(entry)

    async def update(self, runbook_id: UUID, draft: RunbookDraft) -> RunbookView:
        entry = await self.session.scalar(
            select(Runbook)
            .where(Runbook.id == runbook_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry is None:
            raise RunbookNotFound("Runbook 不存在")
        draft = RunbookDraft.model_validate(draft)
        managed = {"success_count", "failure_count", "confidence", "maturity", "automation_level"}
        before = view(entry)
        changed = draft.model_dump(mode="json", exclude=managed) != before.model_dump(
            mode="json", include=set(RunbookDraft.model_fields) - managed
        )
        if not changed:
            if any(getattr(draft, field) != getattr(before, field) for field in managed):
                raise ValueError("计数、可信度和成熟度只能由审核与独立验证服务更新")
            return before
        values = await self._values(draft)
        values.update(
            content_version=entry.content_version + 1,
            success_count=0,
            failure_count=0,
            confidence=0.5,
            maturity=RunbookMaturity.DRAFT.value,
            automation_level=AutomationLevel.MANUAL.value,
        )
        entry = await self.session.scalar(
            update(Runbook)
            .where(Runbook.id == runbook_id)
            .values(**values)
            .returning(Runbook)
            .execution_options(populate_existing=True)
        )
        if entry is None:
            raise RunbookNotFound("Runbook 不存在")
        return view(entry)

    async def delete(self, runbook_id: UUID) -> None:
        value = await self.session.scalar(
            delete(Runbook).where(Runbook.id == runbook_id).returning(Runbook.id)
        )
        if value is None:
            raise RunbookNotFound("Runbook 不存在")

    async def search(self, query: RunbookSearch) -> tuple[RunbookHit, ...]:
        query = RunbookSearch.model_validate(query)
        model, vector = await self._embedding(query.query)
        distance = cast(ColumnElement[float], Runbook.embedding.cosine_distance(vector))
        rows = await self.session.execute(
            select(Runbook, distance)
            .where(Runbook.embedding_model == model, Runbook.embedding_dimensions == len(vector))
            .order_by(distance, Runbook.confidence.desc(), Runbook.id)
            .limit(query.limit)
        )
        hits = []
        for entry, value in rows.tuples():
            if not math.isfinite(value):
                raise ValueError("Runbook 检索返回无效距离")
            hits.append(
                RunbookHit(runbook=view(entry), similarity=max(-1.0, min(1.0, 1.0 - value)))
            )
        return tuple(hits)
