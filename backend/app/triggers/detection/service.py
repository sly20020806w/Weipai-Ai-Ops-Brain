"""同事务记录异常事件、任务、证据和游标；持续异常不重复派发。"""

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger.service import LedgerService
from app.tasks.states import TaskSource
from app.triggers.detection.models import DetectionCursor, DetectionResult, Observation
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventReceipt, NormalizedEvent
from app.triggers.service import EventService


class DetectionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def persist(self, observations: list[Observation], observed_at: str) -> DetectionResult:
        if not self.session.in_transaction():
            raise RuntimeError("检测结果必须显式开启事务")
        values = [Observation.model_validate(value) for value in observations]
        if len({value.detector_key for value in values}) != len(values):
            raise ValueError("同一批次的检测键不能重复")
        locks = sorted({int(value.detector_key[:16], 16) - (1 << 63) for value in values})
        for lock in locks:
            await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        receipts = []
        for value in sorted(values, key=lambda v: v.detector_key):
            cursor = await self.session.scalar(
                select(DetectionCursor).where(DetectionCursor.detector_key == value.detector_key)
            )
            if cursor and value.observed_at < cursor.observed_at:
                # 已提交的旧批次可能丢响应；重试仍需派发当时接受的事件。
                # 未曾接受的过期异常不创建任务，也不改变当前游标。
                if value.breached:
                    previous = await self.session.scalar(
                        select(OpsEvent).where(
                            OpsEvent.origin
                            == ("state" if value.source is TaskSource.STATE else "prediction"),
                            OpsEvent.external_id
                            == f"{value.detector_key}:{value.observed_at.isoformat()}",
                        )
                    )
                    if previous:
                        receipts.append(
                            EventReceipt(
                                str(previous.id),
                                str(previous.task_id),
                                f"ai-task-{previous.task_id}",
                                True,
                            )
                        )
                continue
            if cursor and value.observed_at == cursor.observed_at:
                if value.breached != bool(cursor.active_event_id):
                    raise ValueError("相同采样时间的检测结论冲突")
            if cursor is None:
                cursor = DetectionCursor(
                    detector_key=value.detector_key, observed_at=value.observed_at
                )
                self.session.add(cursor)
            if value.breached:
                if cursor.active_event_id is None:
                    receipt = (
                        await EventService(self.session).accept(
                            [
                                NormalizedEvent(
                                    origin="state"
                                    if value.source is TaskSource.STATE
                                    else "prediction",
                                    source=value.source,
                                    external_id=f"{value.detector_key}:{value.observed_at.isoformat()}",
                                    service_name=value.service_name,
                                    title=value.title,
                                    occurred_at=value.observed_at,
                                )
                            ]
                        )
                    )[0]
                    cursor.active_event_id = UUID(receipt.event_id)
                    await LedgerService(self.session).append_evidence(
                        task_id=UUID(receipt.task_id),
                        source_tool="state_detector"
                        if value.source is TaskSource.STATE
                        else "prediction_detector",
                        parameters={
                            "detector_key": value.detector_key,
                            "service_name": value.service_name,
                        },
                        result_snapshot=value.summary,
                        source_reference=value.source_reference,
                        collected_at=value.observed_at,
                    )
                else:
                    record = await self.session.get(OpsEvent, cursor.active_event_id)
                    assert record is not None
                    receipt = EventReceipt(
                        str(record.id), str(record.task_id), f"ai-task-{record.task_id}", True
                    )
                receipts.append(receipt)
            else:
                cursor.active_event_id = None
            cursor.observed_at = value.observed_at
        await self.session.flush()
        return DetectionResult(observed_at, len(values), receipts)
