"""Temporal 分离持久化熔断与通知；先停动作，再重试发送固定身份的接管通知。"""

from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.feishu.base import FeishuConnector
from app.connectors.feishu.factory import create_feishu_connector
from app.connectors.feishu.models import TextNotification
from app.db.session import Database
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.safety.models import REASON_TEXT, AbortReason, SafetyResult
from app.tasks.safety.service import SafetyStore, abort_record
from app.tasks.workflow_models import TaskSnapshot
from app.tools.registry import json_object


class SafetyActivities:
    def __init__(
        self, database: Database, settings: Settings, *, connector: FeishuConnector | None = None
    ) -> None:
        self.database, self.settings, self.connector = database, settings, connector
        self.store = SafetyStore(database, settings.safety_config)

    @activity.defn(name="safety.check")
    async def check(self, snapshot: TaskSnapshot) -> SafetyResult:
        try:
            return await self.store.check(snapshot)
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("熔断检查版本或证据被拒绝", non_retryable=True) from None

    @activity.defn(name="safety.notify")
    async def notify(self, task_id: str) -> str:
        connector = self.connector or create_feishu_connector(self.settings)
        try:
            return await self.send_notification(task_id, connector)
        finally:
            if self.connector is None:
                await connector.aclose()

    async def send_notification(self, task_id: str, connector: FeishuConnector) -> str:
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == UUID(task_id)).with_for_update()
            )
            record = await abort_record(session, UUID(task_id))
            if task is None or record is None or not isinstance(record.result_snapshot, dict):
                raise ApplicationError("接管通知必须引用已提交的熔断证据", non_retryable=True)
            ledger = LedgerService(session)
            previous = next(
                (
                    e
                    for e in await ledger.evidence_for_task(task.id)
                    if e.source_tool == "safety.notification"
                ),
                None,
            )
            if previous is not None:
                return str(previous.id)
            reasons = record.result_snapshot["reasons"]
            assert isinstance(reasons, list)
            explanation = "、".join(REASON_TEXT[AbortReason(str(r))] for r in reasons)
            notification = TextNotification(
                notification_id=uuid5(NAMESPACE_URL, f"safety/{task.id}/{record.id}"),
                text=(
                    f"自动化已熔断，请人工接管\n任务：{task.id}\n原因：{explanation}\n"
                    f"熔断 Evidence ID：{record.id}\n后续动作已阻止，旧审批不能恢复自动执行。"
                ),
            )
            receipt = await connector.send(notification)
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="safety.notification",
                parameters={"abort_evidence_id": str(record.id)},
                result_snapshot={
                    "notification": json_object(notification.model_dump(mode="json")),
                    "receipt": json_object(receipt.model_dump(mode="json")),
                },
            )
            await ledger.append_audit(
                task_id=task.id,
                event_type=AuditEventType.EXECUTION,
                actor="workflow",
                operation="safety.notify",
                outcome="sent",
                evidence_id=evidence.id,
                details={
                    "abort_evidence_id": str(record.id),
                    "notification_id": str(notification.notification_id),
                },
            )
            return str(evidence.id)
