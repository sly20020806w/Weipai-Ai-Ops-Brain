"""Step 16：本人通知通道；所有真实协议测试只使用 HTTP mock。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.connectors.base import ReadOnlyConnector, WriteConnector
from app.connectors.feishu.base import (
    FeishuAPIError,
    FeishuConnector,
    FeishuError,
    FeishuHTTPError,
    FeishuResponseError,
    FeishuTimeout,
    FeishuTransportError,
)
from app.connectors.feishu.client import HTTPFeishuConnector
from app.connectors.feishu.config import FeishuConfig, FeishuNotificationCredentials
from app.connectors.feishu.factory import create_feishu_connector
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.feishu.models import (
    CardButton,
    CardNotification,
    InteractiveCard,
    NotificationReceipt,
    TextNotification,
)
from app.connectors.models import ConnectorMode, ExecutorCredentials, ReaderCredentials

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
SECRET = "mock-notification-secret"
TOKEN = "mock-tenant-token"


def sample_card() -> InteractiveCard:
    return InteractiveCard(
        title="payment-service 需要补充信息",
        markdown="请确认本次发布是否影响支付业务。此卡片仅演示通知。",
        buttons=(
            CardButton(
                label="查看任务",
                value={"kind": "view_task", "task_id": "sample-task", "context": {"count": 1}},
                style="primary",
            ),
        ),
    )


def real_settings(environment: str = "staging") -> Settings:
    return Settings(
        APP_ENV=environment,
        CONNECTOR_MODE="real",
        FEISHU_CONFIG={"recipient_open_id": "ou_owner", "timeout_seconds": 7},
        FEISHU_NOTIFICATION_CREDENTIALS={"app_id": "cli_notify", "app_secret": SECRET},
    )


def success(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/internal"):
        return httpx.Response(200, json={"code": 0, "tenant_access_token": TOKEN, "expire": 7200})
    return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_sample"}})


@pytest.mark.asyncio
async def test_step16_fake_card_and_text_read_back_complete_content() -> None:
    connector = create_feishu_connector(Settings(APP_ENV="test"))
    assert isinstance(connector, FakeFeishuConnector)
    card = sample_card()
    async with connector:
        text_receipt = await connector.send_text("支付业务告警，等待进一步调查。")
        card_receipt = await connector.send_card(card)
        text, sent_card = connector.sent_messages
        assert text.recipient_open_id == sent_card.recipient_open_id == "ou_fake_owner"
        assert isinstance(text.notification, TextNotification)
        assert text.notification.text == "支付业务告警，等待进一步调查。"
        assert isinstance(sent_card.notification, CardNotification)
        assert sent_card.notification.card == card
        assert sent_card.notification.card.buttons[0].value["task_id"] == "sample-task"
        assert text.receipt == text_receipt
        assert sent_card.receipt == card_receipt
        assert connector.get_sent(card_receipt.notification_id) == sent_card
        assert all(item.receipt.accepted_at.tzinfo is UTC for item in connector.sent_messages)
    # 关闭后仍能读回 Fake 快照，不允许再发送。
    assert len(connector.sent_messages) == 2
    with pytest.raises(FeishuError, match="已关闭"):
        await connector.send_text("不能再发送")


@pytest.mark.asyncio
async def test_fake_idempotency_and_conflicting_content() -> None:
    async with FakeFeishuConnector() as connector:
        notification = TextNotification(text="同一条平台通知")
        receipts = await asyncio.gather(*(connector.send(notification) for _ in range(5)))
        assert all(receipt == receipts[0] for receipt in receipts)
        assert len(connector.sent_messages) == 1
        with pytest.raises(FeishuError, match="不同内容"):
            await connector.send_text("改过的通知", notification_id=notification.notification_id)
        assert len(connector.sent_messages) == 1
        with pytest.raises(KeyError):
            connector.get_sent(uuid4())


@pytest.mark.asyncio
async def test_fake_input_and_output_snapshots_do_not_share_nested_values() -> None:
    async with FakeFeishuConnector() as connector:
        card = sample_card()
        receipt = await connector.send_card(card)
        card.buttons[0].value["task_id"] = "mutated-input"
        first = connector.get_sent(receipt.notification_id)
        assert isinstance(first.notification, CardNotification)
        assert first.notification.card.buttons[0].value["task_id"] == "sample-task"
        first.notification.card.buttons[0].value["task_id"] = "mutated-output"
        from_list = connector.sent_messages[0]
        assert isinstance(from_list.notification, CardNotification)
        assert from_list.notification.card.buttons[0].value["task_id"] == "sample-task"


@pytest.mark.parametrize("environment", ["local", "test", "staging", "production"])
@pytest.mark.asyncio
async def test_default_fake_ignores_real_credentials_and_never_constructs_http(
    environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Fake 不能构建 HTTP 客户端")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", blocked)
    settings = Settings(
        APP_ENV=environment,
        FEISHU_CONFIG={"recipient_open_id": "ou_real_owner"},
        FEISHU_NOTIFICATION_CREDENTIALS={"app_id": "cli_notify", "app_secret": SECRET},
    )
    async with create_feishu_connector(settings) as connector:
        assert isinstance(connector, FakeFeishuConnector)
        assert isinstance(connector, FeishuConnector)
        assert not isinstance(connector, (ReadOnlyConnector, WriteConnector))
        assert not hasattr(connector, "reader_credentials")
        assert not hasattr(connector, "_executor_credentials")
        assert not hasattr(connector, "execute_action")
        await connector.send_text("仅本地通知")
        assert connector.sent_messages[0].recipient_open_id == "ou_fake_owner"


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.asyncio
async def test_real_mode_selects_shared_interface_with_mock(environment: str) -> None:
    connector = create_feishu_connector(
        real_settings(environment), transport=httpx.MockTransport(success)
    )
    assert isinstance(connector, HTTPFeishuConnector)
    async with connector:
        assert (await connector.send_text("mock 发送")).message_id == "om_sample"


@pytest.mark.parametrize("environment", ["local", "test"])
def test_local_real_and_copied_settings_are_rejected(environment: str) -> None:
    with pytest.raises(ValidationError, match="只允许 fake"):
        real_settings(environment)
    copied = Settings(APP_ENV=environment).model_copy(update={"connector_mode": ConnectorMode.REAL})
    with pytest.raises(ValidationError, match="只允许 fake"):
        create_feishu_connector(copied)


@pytest.mark.parametrize("missing", ["feishu_config", "feishu_notification_credentials"])
def test_real_mode_requires_explicit_target_and_notification_credentials(missing: str) -> None:
    settings = real_settings().model_copy(update={missing: None})
    with pytest.raises(ValueError, match="FEISHU_"):
        create_feishu_connector(settings)


def test_environment_config_secrets_and_no_credential_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CONNECTOR_MODE", "real")
    monkeypatch.setenv("FEISHU_CONFIG", '{"recipient_open_id":"ou_owner"}')
    monkeypatch.setenv(
        "FEISHU_NOTIFICATION_CREDENTIALS",
        json.dumps({"app_id": "cli_notify", "app_secret": SECRET}),
    )
    settings = Settings()
    credentials = settings.feishu_notification_credentials
    assert credentials is not None
    assert SECRET not in str(settings) + repr(credentials) + settings.model_dump_json()
    assert settings.require_feishu_config().recipient_open_id == "ou_owner"
    assert not isinstance(credentials, (ReaderCredentials, ExecutorCredentials))


@pytest.mark.parametrize("kind", [ReaderCredentials, ExecutorCredentials])
def test_operational_credentials_cannot_create_notification_client(
    kind: type[ReaderCredentials] | type[ExecutorCredentials],
) -> None:
    credentials = kind(connector="feishu", token=SECRET)
    with pytest.raises(TypeError, match="独立"):
        HTTPFeishuConnector(
            FeishuConfig(recipient_open_id="ou_owner"),
            cast(FeishuNotificationCredentials, credentials),
        )


@pytest.mark.parametrize("secret", ["", " ", "a b", "a\n", "a\x00"])
def test_invalid_notification_secret_rejected_without_echo(secret: str) -> None:
    with pytest.raises(ValidationError, match="app_secret"):
        FeishuNotificationCredentials(app_id="cli_notify", app_secret=secret)


@pytest.mark.parametrize("recipient", ["", "oc_chat", "person@example.com", "ou_a?x", "ou_a\n"])
def test_recipient_must_be_one_fixed_open_id(recipient: str) -> None:
    with pytest.raises(ValidationError):
        FeishuConfig(recipient_open_id=recipient)


@pytest.mark.parametrize("text", ["", " ", "a" * 4097], ids=["empty", "blank", "too-long"])
def test_invalid_text_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        TextNotification(text=text)


def test_card_requires_content_buttons_and_declared_fields() -> None:
    with pytest.raises(ValidationError):
        InteractiveCard(title="标题", markdown="内容", buttons=())
    with pytest.raises(ValidationError):
        sample_card().model_validate({"title": " ", "markdown": "内容", "buttons": ()})
    with pytest.raises(ValidationError):
        TextNotification.model_validate({"text": "通知", "receive_id": "ou_other"})
    with pytest.raises(ValidationError):
        CardButton(label="按钮", value={})


@pytest.mark.parametrize("invalid", ["nan", "oversize", "surrogate", "copied-text", "copied-card"])
@pytest.mark.asyncio
async def test_invalid_payload_rejected_before_fake_or_real_send(invalid: str) -> None:
    card = sample_card()
    if invalid == "nan":
        card.buttons[0].value["bad"] = float("nan")
    elif invalid == "oversize":
        card.buttons[0].value["bad"] = "x" * 21000
    notification: TextNotification | CardNotification = CardNotification(card=card)
    if invalid == "surrogate":
        notification = TextNotification(text="正常").model_copy(update={"text": "\ud800"})
    elif invalid == "copied-text":
        notification = TextNotification(text="正常").model_copy(update={"text": " "})
    elif invalid == "copied-card":
        notification = notification.model_copy(
            update={"card": card.model_copy(update={"title": " "})}
        )
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return success(request)

    async with (
        FakeFeishuConnector() as fake,
        create_feishu_connector(real_settings(), transport=httpx.MockTransport(handler)) as real,
    ):
        for connector in (fake, real):
            with pytest.raises(ValueError):
                await connector.send(notification)
        assert fake.sent_messages == ()
    assert attempts == []


@pytest.mark.parametrize("card", [False, True])
@pytest.mark.asyncio
async def test_http_contract_bound_owner_token_card_encoding_uuid_and_utc(card: bool) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return success(request)

    async with create_feishu_connector(
        real_settings(), transport=httpx.MockTransport(handler)
    ) as connector:
        notification = (
            CardNotification(card=sample_card()) if card else TextNotification(text="支付业务通知")
        )
        receipt = await connector.send(notification)
        assert receipt.message_id == "om_sample"
        assert receipt.notification_id == notification.notification_id
        assert receipt.accepted_at.tzinfo is UTC
    auth, sent = requests
    assert auth.method == sent.method == "POST"
    assert str(auth.url) == "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    assert "authorization" not in auth.headers
    assert json.loads(auth.content) == {"app_id": "cli_notify", "app_secret": SECRET}
    assert (
        str(sent.url) == "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    )
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent.extensions["timeout"] == {"connect": 7, "read": 7, "write": 7, "pool": 7}
    body = json.loads(sent.content)
    assert body["receive_id"] == "ou_owner"
    assert body["uuid"] == str(notification.notification_id)
    assert isinstance(body["content"], str)
    content = json.loads(body["content"])
    if card:
        assert body["msg_type"] == "interactive"
        assert content["header"]["title"]["content"] == sample_card().title
        assert content["elements"][1]["actions"][0]["value"] == sample_card().buttons[0].value
    else:
        assert body["msg_type"] == "text"
        assert content == {"text": "支付业务通知"}


@pytest.mark.parametrize("stage", ["auth", "send"])
@pytest.mark.parametrize("failure", ["timeout", "transport", "http", "redirect", "api", "json"])
@pytest.mark.asyncio
async def test_errors_are_sanitized_no_redirects_or_automatic_retries(
    stage: str, failure: str
) -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        is_auth = request.url.path.endswith("/internal")
        if (stage == "auth") != is_auth:
            return success(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(f"{SECRET} {TOKEN}", request=request)
        if failure == "transport":
            raise httpx.ConnectError(f"{SECRET} {TOKEN}", request=request)
        if failure == "http":
            return httpx.Response(503, text=f"{SECRET} {TOKEN}")
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://example.invalid/leak"})
        if failure == "api":
            return httpx.Response(200, json={"code": 999, "msg": f"{SECRET} {TOKEN}"})
        return httpx.Response(200, text=f"{SECRET} {TOKEN}")

    error_type = {
        "timeout": FeishuTimeout,
        "transport": FeishuTransportError,
        "http": FeishuHTTPError,
        "redirect": FeishuHTTPError,
        "api": FeishuAPIError,
        "json": FeishuResponseError,
    }[failure]
    async with create_feishu_connector(
        real_settings(), transport=httpx.MockTransport(handler)
    ) as connector:
        with pytest.raises(error_type) as caught:
            await connector.send_text("敏感消息内容不应出现在异常中")
    assert SECRET not in str(caught.value) and TOKEN not in str(caught.value)
    assert "敏感消息" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert len(attempts) == (1 if stage == "auth" else 2)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"code": False},
        {"code": "0"},
        {"code": 0},
        {"code": 0, "tenant_access_token": "", "expire": 7200},
        {"code": 0, "tenant_access_token": "token\n", "expire": 7200},
        {"code": 0, "tenant_access_token": TOKEN, "expire": 0},
    ],
)
@pytest.mark.asyncio
async def test_invalid_auth_responses_prevent_message_send(payload: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    async with create_feishu_connector(
        real_settings(), transport=httpx.MockTransport(handler)
    ) as connector:
        with pytest.raises(FeishuResponseError):
            await connector.send_text("发送前 token 校验")
    assert len(requests) == 1


@pytest.mark.parametrize("data", [{}, {"message_id": ""}, {"message_id": 1}])
@pytest.mark.asyncio
async def test_missing_message_ack_is_not_reported_as_success(data: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/internal"):
            return success(request)
        return httpx.Response(200, json={"code": 0, "data": data})

    async with create_feishu_connector(
        real_settings(), transport=httpx.MockTransport(handler)
    ) as connector:
        with pytest.raises(FeishuResponseError, match="结果可能未知"):
            await connector.send_text("结果须有 message_id")


@pytest.mark.asyncio
async def test_http_close_on_exception_and_no_send_after_close() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return success(request)

    connector = create_feishu_connector(real_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="宿主异常"):
        async with connector:
            raise RuntimeError("宿主异常")
    await connector.aclose()
    with pytest.raises(FeishuError, match="已关闭"):
        await connector.send_text("已关闭")
    assert requests == []


def test_receipt_requires_timezone_and_normalizes_utc() -> None:
    args = {"notification_id": uuid4(), "message_id": "om_sample"}
    with pytest.raises(ValidationError, match="时区"):
        NotificationReceipt.model_validate({**args, "accepted_at": datetime(2026, 10, 1)})
    receipt = NotificationReceipt.model_validate(
        {**args, "accepted_at": datetime(2026, 10, 1, 8, tzinfo=timezone(timedelta(hours=8)))}
    )
    assert receipt.accepted_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert receipt.accepted_at.tzinfo is UTC
