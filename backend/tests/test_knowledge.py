"""Step 20 离线输入、向量与失败原子性检查；禁止实际网络连接。"""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic.autogenerate import render_python_code
from alembic.operations.ops import CreateTableOp, UpgradeOps
from pgvector.sqlalchemy import VECTOR  # type: ignore[import-untyped]
from pydantic import ValidationError
from sqlalchemy import Column
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.client import GatewayTimeout
from app.agent.fake import EmbeddingStep, FakeLLM
from app.agent.models import EmbeddingRequest, EmbeddingResponse
from app.db.migrations import render_database_type
from app.knowledge.schemas import KnowledgeDraft, KnowledgePage, KnowledgeSearch, KnowledgeType
from app.knowledge.service import KnowledgeEmbeddingError, KnowledgeService, validate_embedding

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
AT = datetime(2026, 10, 6, tzinfo=UTC)


def draft(content: str = "支付业务高峰期不允许停机", **changes: object) -> KnowledgeDraft:
    return KnowledgeDraft.model_validate(
        {
            "kind": KnowledgeType.BUSINESS_RULE,
            "content": content,
            "source": "业务负责人说明",
            "valid_from": AT,
        }
        | changes
    )


def step(text: str, vector: tuple[float, ...], model: str = "fake-knowledge-v1") -> EmbeddingStep:
    return EmbeddingStep(
        request=EmbeddingRequest(inputs=(text,)),
        response=EmbeddingResponse(model=model, vectors=(vector,)),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "machine_fact"},
        {"content": ""},
        {"content": " \t\n"},
        {"content": "x" * 20001},
        {"source": " "},
        {"source": "x" * 513},
        {"valid_from": datetime(2026, 10, 6)},
        {"expires_at": datetime(2026, 10, 6)},
        {"expires_at": AT},
        {"expires_at": AT - timedelta(seconds=1)},
        {"embedding": [1.0]},
    ],
)
def test_invalid_knowledge_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KnowledgeDraft.model_validate(draft().model_dump() | changes)


def test_knowledge_validity_is_utc_and_open_ended() -> None:
    offset = timezone(timedelta(hours=8))
    entry = draft(
        valid_from=AT.astimezone(offset), expires_at=(AT + timedelta(days=1)).astimezone(offset)
    )
    assert entry.valid_from == AT and entry.valid_from.tzinfo is UTC
    assert entry.expires_at is not None and entry.expires_at.tzinfo is UTC
    assert draft().expires_at is None


@pytest.mark.parametrize(
    "changes",
    [
        {"query": " "},
        {"query": "x" * 20001},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"at": datetime(2026, 10, 6)},
    ],
)
def test_search_is_strict_and_bounded(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KnowledgeSearch.model_validate({"query": "查询规则"} | changes)


@pytest.mark.parametrize("changes", [{"limit": 101}, {"offset": -1}, {"offset": False}])
def test_page_is_strict_and_bounded(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KnowledgePage.model_validate(changes)


@pytest.mark.parametrize(
    "vector",
    [(0.0, 0.0), (1e-100,), (1e100,), (float("inf"),), (float("nan"),), (1.0,) * 16001],
)
def test_invalid_embedding_is_rejected(vector: tuple[float, ...]) -> None:
    with pytest.raises((KnowledgeEmbeddingError, ValidationError)):
        validate_embedding(EmbeddingResponse(model="fake", vectors=(vector,)))


@pytest.mark.parametrize("model", [" ", "m" * 257])
def test_invalid_embedding_model_is_rejected(model: str) -> None:
    with pytest.raises(KnowledgeEmbeddingError):
        validate_embedding(EmbeddingResponse(model=model, vectors=((1.0,),)))


def test_multiple_vectors_are_rejected_and_vectors_are_normalized() -> None:
    with pytest.raises(KnowledgeEmbeddingError):
        validate_embedding(EmbeddingResponse(model="fake", vectors=((1.0,), (2.0,))))
    model, vector = validate_embedding(EmbeddingResponse(model="fake", vectors=((0.1, 1.0),)))
    assert model == "fake" and sum(v * v for v in vector) == pytest.approx(1.0)
    assert vector[0] / vector[1] == pytest.approx(0.1)


@pytest.mark.parametrize("scale", [1e-30, 1e30])
def test_extreme_float32_vectors_are_normalized_for_safe_cosine_distance(scale: float) -> None:
    _, vector = validate_embedding(EmbeddingResponse(model="fake", vectors=((scale, scale),)))
    assert vector == pytest.approx([2**-0.5, 2**-0.5])


@pytest.mark.asyncio
async def test_create_and_update_embedding_failure_do_not_write() -> None:
    session = AsyncMock(spec=AsyncSession)
    value = draft()
    llm = FakeLLM(
        [
            EmbeddingStep(
                request=EmbeddingRequest(inputs=(value.content,)),
                response=GatewayTimeout("模拟超时"),
            ),
            step(value.content, (0.0,)),
        ]
    )
    service = KnowledgeService(session, llm)
    with pytest.raises(GatewayTimeout):
        await service.create(value)
    # update 的初始读成功；零向量仍须在 UPDATE 之前拒绝。
    service.get = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(KnowledgeEmbeddingError):
        await service.update(uuid4(), value)
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
    session.scalar.assert_not_awaited()
    assert llm.remaining_steps == 0


@pytest.mark.asyncio
async def test_model_copy_cannot_bypass_validation_or_call_llm() -> None:
    session = AsyncMock(spec=AsyncSession)
    llm = FakeLLM()
    service = KnowledgeService(session, llm)
    with pytest.raises(ValidationError):
        await service.create(draft().model_copy(update={"content": " "}))
    with pytest.raises(ValidationError):
        await service.search(KnowledgeSearch(query="规则").model_copy(update={"limit": 0}))
    assert not llm.calls
    session.add.assert_not_called()
    session.execute.assert_not_awaited()


def test_vector_autogeneration_has_explicit_import_and_dimension() -> None:
    operations = UpgradeOps(
        [CreateTableOp("probe", [Column("embedding", VECTOR()), Column("fixed", VECTOR(3))])]
    )
    code = render_python_code(operations, render_item=render_database_type)
    assert "VECTOR()" in code and "VECTOR(3)" in code
    assert "pgvector.sqlalchemy.vector.VECTOR" not in code
