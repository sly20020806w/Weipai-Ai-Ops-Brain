"""事件、任务、初始状态及审计同事务创建，数据库锁负责并发去重。"""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.service import TaskService
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventReceipt, NormalizedEvent


class EventService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def accept(self, events: list[NormalizedEvent]) -> list[EventReceipt]:
        if not self.session.in_transaction():
            raise RuntimeError("事件接入必须显式开启事务")
        values = {event.fingerprint: NormalizedEvent.model_validate(event) for event in events}
        receipts = []
        # 固定锁顺序避免多告警批次的交叉锁死；hash 碰撞只会多串行，不影响正确性。
        locks = sorted({int(key[:16], 16) - (1 << 63) for key in values})
        for lock in locks:
            await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        for fingerprint, event in sorted(values.items()):
            record = await self.session.scalar(
                select(OpsEvent).where(OpsEvent.fingerprint == fingerprint)
            )
            duplicate = record is not None
            if record is None:
                task = await TaskService(self.session).create(
                    source=event.source,
                    title=event.title,
                    reason=f"由 OpsEvent 接入：{event.origin}/{event.external_id}",
                )
                record = OpsEvent(
                    fingerprint=fingerprint,
                    task_id=task.id,
                    origin=event.origin,
                    source=event.source.value,
                    external_id=event.external_id,
                    service_name=event.service_name,
                    title=event.title,
                    occurred_at=event.occurred_at,
                )
                self.session.add(record)
                await self.session.flush()
            receipts.append(
                EventReceipt(
                    str(record.id), str(record.task_id), f"ai-task-{record.task_id}", duplicate
                )
            )
        return receipts
