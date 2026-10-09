"""已登录对话的 SSE HTTP 适配；业务逻辑由 Agent/任务服务持有。"""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError, FailureError
from temporalio.service import RPCError

from app.agent.chat.gateway import ChatGateway
from app.agent.chat.models import ChatAnswer, ChatInput, ChatSubmission
from app.api.auth import CurrentPrincipal
from app.api.dependencies import get_session
from app.config import Settings
from app.db.session import Database
from app.tasks.console_queries import ConsoleConflict, ConsoleNotFound
from app.triggers.schemas import EventReceipt

router = APIRouter(
    prefix="/api/chat",
    tags=["AI 对话"],
    responses={
        401: {"description": "请先登录"},
        404: {"description": "对话记录不存在"},
        409: {"description": "对话输入或证据身份冲突"},
        503: {"description": "存储或 Temporal 暂时不可用"},
    },
)


async def get_chat_gateway(request: Request) -> ChatGateway:
    settings: Settings = request.app.state.settings
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(503, "对话需要 DATABASE_URL")
    from app.tasks.worker import validate_placeholder_settings

    try:
        validate_placeholder_settings(settings)
        client = await Client.connect(
            settings.temporal_config.address, namespace=settings.temporal_config.namespace
        )
    except (ValueError, RuntimeError, RPCError, OSError, TimeoutError):
        raise HTTPException(503, "对话任务暂时不可用") from None
    return ChatGateway(client, database, settings)


Gateway = Annotated[ChatGateway, Depends(get_chat_gateway)]


def frame(event: str, payload: dict[str, object], sequence: int) -> str:
    return f"id: {sequence}\nevent: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream(gateway: ChatGateway, receipt: EventReceipt) -> AsyncGenerator[str, None]:
    from dataclasses import asdict

    receipt_json = asdict(receipt)
    yield frame("task", receipt_json, 0)
    pending = asyncio.create_task(gateway.wait(receipt))
    try:
        # 只等待同一个 Temporal Update；heartbeat 不查询或迁移任务状态。
        async with asyncio.timeout(gateway.settings.chat_stream_timeout_seconds):
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=10)
                if not done:
                    yield ": heartbeat\n\n"
            answer = await pending
        sequence = 1
        if answer.answer is not None:
            yield frame(
                "evidence", {"evidence_ids": [str(e) for e in answer.evidence_ids]}, sequence
            )
            sequence += 1
            for offset in range(0, len(answer.answer), 120):
                yield frame("delta", {"text": answer.answer[offset : offset + 120]}, sequence)
                sequence += 1
        yield frame("done", answer.model_dump(mode="json"), sequence)
    except (
        TimeoutError,
        RPCError,
        WorkflowFailureError,
        SQLAlchemyError,
        ConsoleConflict,
        ConsoleNotFound,
        ValueError,
        OSError,
    ):
        yield frame(
            "error",
            {
                "task_id": receipt.task_id,
                "message": "回答暂时不可用；任务已保留，可使用相同 request_id 重试或查询任务",
            },
            1,
        )
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@router.post(
    "",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "task/evidence/delta/done 或 error 事件",
        },
        404: {"description": "上一轮对话不存在"},
        409: {"description": "相同请求 ID 的内容冲突"},
        503: {"description": "任务接入暂时不可用"},
    },
)
async def chat(
    body: ChatInput,
    principal: CurrentPrincipal,
    gateway: Gateway,
    csrf_token: Annotated[str, Header(alias="X-CSRF-Token")],
) -> StreamingResponse:
    try:
        receipt = await gateway.submit(ChatSubmission(input=body, actor=principal.actor))
    except WorkflowFailureError as error:
        cause: BaseException | None = error.cause
        while cause is not None and not isinstance(cause, ApplicationError):
            cause = cause.cause if isinstance(cause, FailureError) else None
        status = {"ChatNotFound": 404, "ChatConflict": 409, "ChatInvalid": 422}.get(
            (cause.type or "") if isinstance(cause, ApplicationError) else "", 503
        )
        raise HTTPException(status, "对话请求被拒绝或尚未确认；相同请求可以安全重试") from None
    except (TimeoutError, RPCError, OSError):
        raise HTTPException(503, "任务接入尚未确认，请使用相同 request_id 重试") from None
    return StreamingResponse(
        stream(gateway, receipt),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


@router.get("/{task_id}")
async def chat_answer(
    task_id: UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> ChatAnswer:
    from app.agent.chat.service import read_answer

    return await read_answer(session, task_id)
