"""飞书自建应用 HTTP 通知适配器；仅获取短时 token 与向本人发送消息。"""

from datetime import UTC, datetime

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from app.connectors.feishu.base import (
    FeishuAPIError,
    FeishuConnector,
    FeishuError,
    FeishuHTTPError,
    FeishuResponseError,
    FeishuTimeout,
    FeishuTransportError,
)
from app.connectors.feishu.config import FeishuConfig, FeishuNotificationCredentials
from app.connectors.feishu.models import (
    Notification,
    NotificationReceipt,
    checked_notification,
    notification_content,
)


class _Envelope(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    code: int


class _TokenResponse(_Envelope):
    tenant_access_token: SecretStr
    expire: int = Field(gt=0)

    @field_validator("tenant_access_token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if not token or any(char.isspace() or ord(char) < 32 for char in token):
            raise ValueError("飞书 token 无效")
        return value


class _MessageData(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    message_id: str = Field(pattern=r"^om_[A-Za-z0-9_-]{1,128}$")


class _MessageResponse(_Envelope):
    data: _MessageData


class HTTPFeishuConnector(FeishuConnector):
    def __init__(
        self,
        config: FeishuConfig,
        credentials: FeishuNotificationCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        if not isinstance(credentials, FeishuNotificationCredentials):
            raise TypeError("飞书通知只接受独立的 FeishuNotificationCredentials")
        self._config = FeishuConfig.model_validate(config)
        self._credentials = FeishuNotificationCredentials.model_validate(credentials)
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TypeError("测试 transport 只接受 MockTransport")
        self._http = httpx.AsyncClient(
            base_url="https://open.feishu.cn/open-apis/",
            headers={"Accept": "application/json"},
            timeout=self._config.timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(
        self,
        path: str,
        body: dict[str, str],
        *,
        token: SecretStr | None = None,
        params: dict[str, str] | None = None,
    ) -> bytes:
        if self._http.is_closed:
            raise FeishuError("飞书通知 Connector 已关闭")
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"} if token else {}
        try:
            response = await self._http.post(path, json=body, headers=headers, params=params)
        except httpx.TimeoutException:
            raise FeishuTimeout("飞书通知请求超时；发送结果可能未知") from None
        except httpx.RequestError:
            raise FeishuTransportError("飞书通知连接失败；发送结果可能未知") from None
        if not response.is_success:
            raise FeishuHTTPError(response.status_code)
        try:
            envelope = _Envelope.model_validate_json(response.content)
        except ValidationError:
            raise FeishuResponseError("飞书通知响应格式无效") from None
        if envelope.code != 0:
            raise FeishuAPIError(envelope.code)
        return response.content

    async def send(self, notification: Notification) -> NotificationReceipt:
        if self._http.is_closed:
            raise FeishuError("飞书通知 Connector 已关闭")
        snapshot = checked_notification(notification)
        content = notification_content(snapshot)
        # 不建立自己的续期循环或重试调度；token 仅在当前发送中使用。
        token_body = await self._post(
            "auth/v3/tenant_access_token/internal",
            {
                "app_id": self._credentials.app_id,
                "app_secret": self._credentials.app_secret.get_secret_value(),
            },
        )
        try:
            token = _TokenResponse.model_validate_json(token_body).tenant_access_token
        except ValidationError:
            raise FeishuResponseError("飞书短时 token 响应格式无效") from None
        result = await self._post(
            "im/v1/messages",
            {
                "receive_id": self._config.recipient_open_id,
                "msg_type": snapshot.msg_type,
                "content": content,
                "uuid": str(snapshot.notification_id),
            },
            token=token,
            params={"receive_id_type": "open_id"},
        )
        try:
            message = _MessageResponse.model_validate_json(result)
        except ValidationError:
            raise FeishuResponseError("飞书发送确认格式无效；发送结果可能未知") from None
        return NotificationReceipt(
            notification_id=snapshot.notification_id,
            message_id=message.data.message_id,
            accepted_at=datetime.now(UTC),
        )
