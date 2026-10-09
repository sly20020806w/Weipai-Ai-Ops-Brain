"""验签 Webhook HTTP 适配；归一化、去重与派发由 triggers 负责。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from temporalio.client import Client, WorkflowFailureError
from temporalio.service import RPCError

from app.config import Settings
from app.triggers.gateway import EventGateway
from app.triggers.normalization import (
    PayloadError,
    SignatureError,
    normalize_webhook,
    verify_signature,
)
from app.triggers.schemas import EventOrigin, EventReceipt, NormalizedEvent

router = APIRouter(prefix="/webhooks", tags=["事件接入"])


async def read_signed_events(origin: EventOrigin, request: Request) -> list[NormalizedEvent]:
    settings: Settings = request.app.state.settings
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.trigger_config.max_body_bytes:
            raise HTTPException(413, "事件请求体过大")
    try:
        verify_signature(
            settings.trigger_config,
            origin,
            bytes(body),
            request.headers.get("X-Ops-Timestamp", ""),
            request.headers.get("X-Ops-Signature", ""),
        )
        events = normalize_webhook(origin, bytes(body))
    except SignatureError:
        raise HTTPException(401, "Webhook 签名无效") from None
    except PayloadError as error:
        raise HTTPException(422, str(error)) from None
    return events


SignedEvents = Annotated[list[NormalizedEvent], Depends(read_signed_events)]


async def get_event_gateway(request: Request, events: SignedEvents) -> EventGateway | None:
    if not events:
        return None
    settings: Settings = request.app.state.settings
    if settings.database_url is None:
        raise HTTPException(503, "事件接入需要 DATABASE_URL")
    from app.tasks.worker import validate_placeholder_settings

    try:
        validate_placeholder_settings(settings)
        client = await Client.connect(
            settings.temporal_config.address, namespace=settings.temporal_config.namespace
        )
    except (ValueError, RuntimeError, RPCError):
        raise HTTPException(503, "本地 Temporal 事件接入尚未就绪") from None
    return EventGateway(client, settings)


class WebhookResponse(BaseModel):
    events: list[EventReceipt]


@router.post("/{origin}", status_code=202)
async def receive_event(
    events: SignedEvents, gateway: Annotated[EventGateway | None, Depends(get_event_gateway)]
) -> WebhookResponse:
    if not events:
        return WebhookResponse(events=[])
    assert gateway is not None
    try:
        receipts = await gateway.submit(events)
    except (TimeoutError, RPCError, WorkflowFailureError):
        raise HTTPException(
            503, "Temporal 接入处理中或暂时不可用，请重试；指纹会防止重复任务"
        ) from None
    return WebhookResponse(events=receipts)
