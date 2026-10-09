"""有限的文本/交互卡片协议；按钮只携带回调数据，不执行审批或运维动作。"""

import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator


class NotificationModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class CardButton(NotificationModel):
    label: str = Field(min_length=1, max_length=100)
    value: dict[str, JsonValue] = Field(min_length=1)
    style: Literal["default", "primary", "danger"] = "default"

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("卡片按钮文字不能为空")
        return value


class InteractiveCard(NotificationModel):
    title: str = Field(min_length=1, max_length=128)
    markdown: str = Field(min_length=1, max_length=4096)
    buttons: tuple[CardButton, ...] = Field(min_length=1, max_length=5)

    @field_validator("title", "markdown")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("卡片标题与内容不能为空")
        return value


class TextNotification(NotificationModel):
    msg_type: Literal["text"] = "text"
    notification_id: UUID = Field(default_factory=uuid4)
    text: str = Field(min_length=1, max_length=4096)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("通知文本不能为空")
        return value


class CardNotification(NotificationModel):
    msg_type: Literal["interactive"] = "interactive"
    notification_id: UUID = Field(default_factory=uuid4)
    card: InteractiveCard


Notification = Annotated[TextNotification | CardNotification, Field(discriminator="msg_type")]
_notification: TypeAdapter[TextNotification | CardNotification] = TypeAdapter(Notification)


def checked_notification(value: Notification) -> Notification:
    # 不信任 model_copy；深复制嵌套按钮数据，发送与 Fake 快照不会共享可变字典。
    return _notification.validate_python(value).model_copy(deep=True)


def notification_content(notification: Notification) -> str:
    if isinstance(notification, TextNotification):
        content: dict[str, JsonValue] = {"text": notification.text}
    else:
        card = notification.card
        content = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": card.title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": card.markdown}},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": button.label},
                            "type": button.style,
                            "value": button.value,
                        }
                        for button in card.buttons
                    ],
                },
            ],
        }
    try:
        encoded = json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("通知内容必须为有效 UTF-8 JSON，且不含非有限数值") from None
    # 本平台主动使用更小的上限，包含按钮回调数据。
    if size > 20 * 1024:
        raise ValueError("通知内容超过本平台 20 KiB 上限")
    return encoded


class NotificationReceipt(NotificationModel):
    notification_id: UUID
    message_id: str = Field(min_length=1, max_length=200)
    accepted_at: datetime

    @field_validator("accepted_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("通知接收确认时间必须带时区")
        return value.astimezone(UTC)


class SentNotification(NotificationModel):
    recipient_open_id: str
    notification: Notification
    receipt: NotificationReceipt
