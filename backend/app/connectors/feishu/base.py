"""平台通知接口：不属于 Agent Tool 或生产动作 Executor。"""

from abc import abstractmethod
from uuid import UUID

from app.connectors.base import Connector
from app.connectors.feishu.models import (
    CardNotification,
    InteractiveCard,
    Notification,
    NotificationReceipt,
    TextNotification,
)


class FeishuError(RuntimeError):
    """飞书异常只保留安全错误类别，不携带消息、凭证或源系统响应。"""


class FeishuHTTPError(FeishuError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"飞书通知返回 HTTP {status_code}")


class FeishuAPIError(FeishuError):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"飞书通知返回业务错误码 {code}")


class FeishuResponseError(FeishuError):
    pass


class FeishuTimeout(FeishuError):
    pass


class FeishuTransportError(FeishuError):
    pass


class FeishuConnector(Connector):
    @abstractmethod
    async def send(self, notification: Notification) -> NotificationReceipt: ...

    async def send_text(
        self, text: str, *, notification_id: UUID | None = None
    ) -> NotificationReceipt:
        notification = (
            TextNotification(text=text)
            if notification_id is None
            else TextNotification(text=text, notification_id=notification_id)
        )
        return await self.send(notification)

    async def send_card(
        self, card: InteractiveCard, *, notification_id: UUID | None = None
    ) -> NotificationReceipt:
        notification = (
            CardNotification(card=card)
            if notification_id is None
            else CardNotification(card=card, notification_id=notification_id)
        )
        return await self.send(notification)
