"""内存 Fake 发件箱：完整读回内容、固定本人收件人、同 ID 重发去重。"""

from datetime import UTC, datetime
from uuid import UUID

from app.connectors.feishu.base import FeishuConnector, FeishuError
from app.connectors.feishu.config import FeishuConfig
from app.connectors.feishu.models import (
    Notification,
    NotificationReceipt,
    SentNotification,
    checked_notification,
    notification_content,
)


class FakeFeishuConnector(FeishuConnector):
    def __init__(self, *, recipient_open_id: str = "ou_fake_owner") -> None:
        self._recipient = FeishuConfig(recipient_open_id=recipient_open_id).recipient_open_id
        self._sent: dict[UUID, SentNotification] = {}
        self._closed = False

    @property
    def sent_messages(self) -> tuple[SentNotification, ...]:
        return tuple(record.model_copy(deep=True) for record in self._sent.values())

    def get_sent(self, notification_id: UUID) -> SentNotification:
        return self._sent[notification_id].model_copy(deep=True)

    async def send(self, notification: Notification) -> NotificationReceipt:
        if self._closed:
            raise FeishuError("Fake 飞书通知 Connector 已关闭")
        snapshot = checked_notification(notification)
        notification_content(snapshot)
        previous = self._sent.get(snapshot.notification_id)
        if previous is not None:
            if previous.notification != snapshot:
                raise FeishuError("同一 notification_id 不能发送不同内容")
            return previous.receipt.model_copy(deep=True)
        receipt = NotificationReceipt(
            notification_id=snapshot.notification_id,
            message_id=f"fake_{snapshot.notification_id}",
            accepted_at=datetime.now(UTC),
        )
        self._sent[snapshot.notification_id] = SentNotification(
            recipient_open_id=self._recipient, notification=snapshot, receipt=receipt
        )
        return receipt.model_copy(deep=True)

    async def aclose(self) -> None:
        self._closed = True
