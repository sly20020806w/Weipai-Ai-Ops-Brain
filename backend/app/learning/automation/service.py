"""统计与建议同事务；固定分组身份让并发、重试和滑动窗口均不会重复建任务。"""

from datetime import timedelta
from uuid import UUID

from pydantic import AwareDatetime, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utc_now
from app.learning.automation.models import (
    LABELS,
    METHOD_LABELS,
    METHODS,
    AutomationConfig,
    AutomationResult,
    ManualOperation,
    Repetition,
    ScanWindow,
    Suggestion,
    group_records,
)
from app.learning.automation.sources import collect_records
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.states import TaskSource
from app.tools.registry import json_object
from app.triggers.models import OpsEvent
from app.triggers.schemas import NormalizedEvent
from app.triggers.service import EventService


def method_for(group: Repetition) -> str:
    if group.records[0].signature.startswith("question:"):
        return "workflow"  # 业务判断保留人工，只评估收集/流转的自动化。
    return METHODS[group.records[0].kind]


class AutomationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.ledger = LedgerService(session)

    async def record_manual(self, value: ManualOperation) -> UUID:
        """宿主记录已发生的人工劳动，调用本方法不会执行该操作。"""
        value = ManualOperation.model_validate(value)
        if not self.session.in_transaction():
            raise RuntimeError("人工操作记录必须在事务内保存")
        if value.occurred_at > utc_now():
            raise ValueError("不能记录未来的人工操作")
        task = await self.session.scalar(
            select(AITask).where(AITask.id == value.task_id).with_for_update()
        )
        if task is None:
            raise ValueError("人工操作任务不存在")
        event = await self.session.scalar(select(OpsEvent).where(OpsEvent.task_id == task.id))
        if event is not None and (
            event.origin == "learning" or event.service_name != value.service_name
        ):
            raise ValueError("人工操作与来源服务不一致或属于建议任务")
        cached = await self.session.scalar(
            select(Evidence).where(
                Evidence.task_id == task.id,
                Evidence.source_tool == "manual.operation",
                Evidence.parameters["record_key"].as_string() == value.record_key,
            )
        )
        if cached is not None:
            if cached.result_snapshot != json_object(value.model_dump(mode="json")):
                raise ValueError("同一人工操作记录的内容冲突")
            return cached.id
        evidence = await self.ledger.append_evidence(
            task_id=task.id,
            source_tool="manual.operation",
            parameters={"record_key": value.record_key},
            result_snapshot=json_object(value.model_dump(mode="json")),
            source_reference=value.source_reference,
            collected_at=value.occurred_at,
        )
        await self.ledger.append_audit(
            task_id=task.id,
            actor=value.actor,
            event_type=AuditEventType.HUMAN_INTERACTION,
            operation="manual.operation",
            outcome="recorded",
            evidence_id=evidence.id,
            occurred_at=value.occurred_at,
            details={"record_key": value.record_key},
        )
        return evidence.id

    async def scan(self, config: AutomationConfig, end: str) -> AutomationResult:
        config = AutomationConfig.model_validate(config)
        # 日期仅由 Temporal 或宿主提供，显式拒绝无时区与未来截止点。
        cutoff = TypeAdapter(AwareDatetime).validate_python(end)
        window = ScanWindow(start=cutoff - timedelta(seconds=config.lookback_seconds), end=cutoff)
        if window.end > utc_now():
            raise ValueError("扫描不能使用未来截止点")
        if not self.session.in_transaction():
            raise RuntimeError("自动化发现必须在事务内运行")
        if not config.enabled:
            return AutomationResult(0, [], [])
        records = await collect_records(self.session, window)
        groups = group_records(records, config.threshold)
        events = [
            NormalizedEvent(
                origin="learning",
                source=TaskSource.AI,
                external_id=f"automation:{group.group_key}",
                service_name=group.records[0].service_name,
                title=(
                    f"自动化建议：{LABELS[group.records[0].kind]}累计 {len(group.records)} 次，"
                    f"{METHOD_LABELS[method_for(group)]}"
                ),
                occurred_at=window.end,
            )
            for group in groups
        ]
        receipts = await EventService(self.session).accept(events)
        by_key = {event.fingerprint: group for event, group in zip(events, groups, strict=True)}
        ids = []
        for receipt in receipts:
            event = await self.session.get(OpsEvent, UUID(receipt.event_id))
            assert event is not None
            cached = await self.session.scalar(
                select(Evidence).where(
                    Evidence.task_id == event.task_id,
                    Evidence.source_tool == "automation.suggestion",
                )
            )
            if cached is not None:
                ids.append(str(cached.id))
                continue
            group = by_key[event.fingerprint]
            method = method_for(group)
            suggestion = Suggestion.model_validate(
                {
                    "repetition": group,
                    "method": method,
                    "conclusion": (
                        f"同一服务的同类劳动已发生 {len(group.records)} 次，"
                        f"{METHOD_LABELS[method]}。"
                    ),
                }
            )
            saved = await self.ledger.append_evidence(
                task_id=event.task_id,
                source_tool="automation.suggestion",
                parameters={"group_key": group.group_key},
                result_snapshot=json_object(suggestion.model_dump(mode="json")),
                source_reference=f"ops-event:{event.id}",
                collected_at=window.end,
            )
            ids.append(str(saved.id))
        return AutomationResult(len(records), receipts, ids)
