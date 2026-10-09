"""证据 ID 精确引用、任务时间序查询及审计追加；外层事务由调用方提交。"""

import json
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import UTCDateTime, utc_now
from app.ledger.models import AuditEventType, AuditRecord, Evidence


class EvidenceNotFound(LookupError):
    pass


def _required_text(value: str, field: str, max_length: int) -> str:
    value = value.strip()
    if not value or len(value) > max_length:
        raise ValueError(f"{field} 必须非空且符合长度限制")
    return value


def _json_copy(value: JsonValue) -> JsonValue:
    # 拒绝 NaN、Infinity、非 JSON 对象和非字符串键；复制嵌套值以隔离调用方修改。
    def validate(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON 对象的键必须为字符串")
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ValueError("只能保存有效的 JSON 数据")

    validate(value)
    try:
        return cast(JsonValue, json.loads(json.dumps(value, allow_nan=False)))
    except (TypeError, ValueError) as error:
        raise ValueError("只能保存有效的 JSON 数据") from error


def _json_object(value: dict[str, JsonValue], field: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须为 JSON 对象")
    return cast(dict[str, JsonValue], _json_copy(value))


def _utc_time(value: datetime | None) -> datetime:
    normalized = UTCDateTime.normalize(value)
    return utc_now() if normalized is None else normalized


class LedgerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _require_transaction(self) -> None:
        if not self.session.in_transaction():
            raise RuntimeError("调用 ledger 服务前请使用 async with session.begin() 开启事务")

    async def append_evidence(
        self,
        *,
        task_id: UUID,
        source_tool: str,
        parameters: dict[str, JsonValue],
        result_snapshot: JsonValue | None = None,
        source_reference: str | None = None,
        collected_at: datetime | None = None,
    ) -> Evidence:
        self._require_transaction()
        source_tool = _required_text(source_tool, "source_tool", 200)
        if source_reference is not None:
            source_reference = source_reference.strip()
            if not source_reference:
                raise ValueError("source_reference 必须非空")
        if result_snapshot is None and source_reference is None:
            raise ValueError("证据必须包含结果快照或源系统引用")
        record = Evidence(
            id=uuid4(),
            task_id=task_id,
            source_tool=source_tool,
            parameters=_json_object(parameters, "parameters"),
            result_snapshot=_json_copy(result_snapshot),
            source_reference=source_reference,
            collected_at=_utc_time(collected_at),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_evidence(self, evidence_id: UUID) -> Evidence:
        record = await self.session.get(Evidence, evidence_id)
        if record is None:
            raise EvidenceNotFound(f"证据不存在：{evidence_id}")
        return record

    async def evidence_for_task(self, task_id: UUID) -> list[Evidence]:
        records = await self.session.scalars(
            select(Evidence)
            .where(Evidence.task_id == task_id)
            .order_by(Evidence.collected_at, Evidence.id)
        )
        return list(records)

    async def append_audit(
        self,
        *,
        task_id: UUID,
        event_type: AuditEventType,
        actor: str,
        operation: str,
        outcome: str,
        details: dict[str, JsonValue],
        evidence_id: UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditRecord:
        self._require_transaction()
        record = AuditRecord(
            id=uuid4(),
            task_id=task_id,
            event_type=AuditEventType(event_type),
            actor=_required_text(actor, "actor", 200),
            operation=_required_text(operation, "operation", 200),
            outcome=_required_text(outcome, "outcome", 100),
            details=_json_object(details, "details"),
            evidence_id=evidence_id,
            occurred_at=_utc_time(occurred_at),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def audits_for_task(self, task_id: UUID) -> list[AuditRecord]:
        records = await self.session.scalars(
            select(AuditRecord)
            .where(AuditRecord.task_id == task_id)
            .order_by(AuditRecord.occurred_at, AuditRecord.id)
        )
        return list(records)
