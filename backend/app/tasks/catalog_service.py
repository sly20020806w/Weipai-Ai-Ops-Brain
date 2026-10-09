"""本人维护平台知识与 Runbook；内容、向量和编辑审计在同一事务提交。"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import EmbeddingRequest
from app.config import Settings
from app.knowledge.models import KnowledgeEntry
from app.knowledge.schemas import KnowledgeDraft, KnowledgeView
from app.knowledge.service import KnowledgeNotFound, KnowledgeService
from app.ledger.models import CatalogAudit
from app.runbooks.embedding import embedding_client
from app.runbooks.models import Runbook
from app.runbooks.schemas import AutomationLevel, RunbookDraft, RunbookMaturity, RunbookView
from app.runbooks.service import RunbookNotFound, RunbookService
from app.tasks.console_queries import ConsoleConflict
from app.tasks.operations_models import KnowledgeInput, RunbookInput


class CatalogService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session, self.settings = session, settings

    async def audit(self, resource: str, verb: str, identity: UUID, actor: str) -> None:
        if not actor.strip() or len(actor) > 200:
            raise ValueError("操作人无效")
        self.session.add(
            CatalogAudit(
                actor=actor, operation=f"{resource}.{verb}", details={"record_id": str(identity)}
            )
        )
        await self.session.flush()

    async def knowledge(
        self, verb: str, body: KnowledgeInput | None, identity: UUID | None, actor: str
    ) -> KnowledgeView | None:
        llm = None
        try:
            async with self.session.begin():
                if identity is not None:
                    entry = await self.session.scalar(
                        select(KnowledgeEntry)
                        .where(KnowledgeEntry.id == identity)
                        .with_for_update()
                    )
                    if entry is None:
                        raise KnowledgeNotFound("知识条目不存在")
                if verb == "delete":
                    assert identity is not None
                    # 删除不需要模型或外部网关。
                    from app.agent.fake import FakeLLM

                    await KnowledgeService(self.session, FakeLLM()).delete(identity)
                    result = None
                else:
                    assert body is not None
                    draft = KnowledgeDraft.model_validate_json(body.model_dump_json())
                    llm = embedding_client(self.settings, EmbeddingRequest(inputs=(draft.content,)))
                    service = KnowledgeService(self.session, llm)
                    result = (
                        await service.create(draft)
                        if identity is None
                        else await service.update(identity, draft)
                    )
                    identity = result.id
                assert identity is not None
                await self.audit("knowledge", verb, identity, actor)
                return result
        finally:
            if llm is not None:
                await llm.aclose()

    async def runbook(
        self, verb: str, body: RunbookInput | None, identity: UUID | None, actor: str
    ) -> RunbookView | None:
        try:
            return await self._runbook(verb, body, identity, actor)
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise ConsoleConflict("Runbook 名称已存在") from None
            raise

    async def _runbook(
        self, verb: str, body: RunbookInput | None, identity: UUID | None, actor: str
    ) -> RunbookView | None:
        async with self.session.begin():
            service = RunbookService(self.session, lambda r: embedding_client(self.settings, r))
            if verb == "delete":
                assert identity is not None
                await service.delete(identity)
                result = None
            else:
                assert body is not None
                data = body.model_dump(mode="json")
                if identity is not None:
                    locked = await self.session.scalar(
                        select(Runbook).where(Runbook.id == identity).with_for_update()
                    )
                    if locked is None:
                        raise RunbookNotFound("Runbook 不存在")
                before = await service.get(identity) if identity is not None else None
                data.update(
                    {
                        field: getattr(before, field) if before else value
                        for field, value in {
                            "success_count": 0,
                            "failure_count": 0,
                            "confidence": 0.5,
                            "automation_level": AutomationLevel.MANUAL,
                            "maturity": RunbookMaturity.DRAFT,
                        }.items()
                    }
                )
                draft = RunbookDraft.model_validate_json(json.dumps(data))
                result = (
                    await service.create(draft)
                    if identity is None
                    else await service.update(identity, draft)
                )
                identity = result.id
            assert identity is not None
            await self.audit("runbooks", verb, identity, actor)
            return result
