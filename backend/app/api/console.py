"""任务闭环 HTTP 适配；查询和操作均由业务服务完成。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client, WorkflowFailureError
from temporalio.service import RPCError

from app.api.auth import CurrentPrincipal
from app.api.dependencies import get_session
from app.config import Settings
from app.db.session import Database
from app.learning.models import IncidentHit
from app.ledger.models import Evidence
from app.tasks.console_models import (
    AnswerInput,
    ApprovalInput,
    ControlCommand,
    ControlReceipt,
    EventView,
    EvidenceView,
    HistoryView,
    InteractionView,
    Page,
    TakeoverInput,
    TaskView,
    ToolCallView,
)
from app.tasks.console_queries import ConsoleQueries
from app.tasks.control import ControlGateway
from app.tasks.models import TaskStatusHistory
from app.tasks.states import TaskSource, TaskStatus
from app.tools.registry import json_object
from app.triggers.schemas import EventOrigin

router = APIRouter(
    prefix="/api",
    tags=["任务闭环"],
    responses={
        401: {"description": "请先登录"},
        404: {"description": "记录不存在"},
        409: {"description": "等待版本或操作内容冲突"},
        503: {"description": "本地存储或 Temporal 暂时不可用"},
    },
)
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
ServiceName = Annotated[str | None, Query(min_length=1, max_length=256)]
Csrf = Annotated[str, Header(alias="X-CSRF-Token")]


def get_queries(session: Annotated[AsyncSession, Depends(get_session)]) -> ConsoleQueries:
    return ConsoleQueries(session)


Queries = Annotated[ConsoleQueries, Depends(get_queries)]


async def get_control_gateway(request: Request) -> ControlGateway:
    settings: Settings = request.app.state.settings
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(503, "任务操作需要 DATABASE_URL")
    from app.tasks.worker import validate_placeholder_settings

    try:
        validate_placeholder_settings(settings)
        client = await Client.connect(
            settings.temporal_config.address, namespace=settings.temporal_config.namespace
        )
    except (ValueError, RuntimeError, RPCError, OSError, TimeoutError):
        raise HTTPException(503, "Temporal 任务操作暂时不可用") from None
    return ControlGateway(client, settings, database)


Gateway = Annotated[ControlGateway, Depends(get_control_gateway)]


@router.get("/tasks")
async def list_tasks(
    queries: Queries,
    limit: Limit = 50,
    offset: Offset = 0,
    status: TaskStatus | None = None,
    source: TaskSource | None = None,
) -> Page[TaskView]:
    return await queries.tasks(limit, offset, status, source)


@router.get("/tasks/{task_id}")
async def task_detail(task_id: UUID, queries: Queries) -> TaskView:
    return TaskView.model_validate(await queries.task(task_id))


@router.get("/tasks/{task_id}/interaction")
async def interaction(task_id: UUID, queries: Queries) -> InteractionView:
    return await queries.interaction(task_id)


@router.get("/events")
async def list_events(
    queries: Queries,
    limit: Limit = 50,
    offset: Offset = 0,
    source: TaskSource | None = None,
    origin: EventOrigin | None = None,
    service_name: ServiceName = None,
) -> Page[EventView]:
    return await queries.events(limit, offset, source, origin, service_name)


@router.get("/events/{event_id}")
async def event_detail(event_id: UUID, queries: Queries) -> EventView:
    return EventView.model_validate(await queries.event(event_id))


@router.get("/evidence")
async def list_evidence(
    queries: Queries, limit: Limit = 50, offset: Offset = 0, task_id: UUID | None = None
) -> Page[EvidenceView]:
    return await queries.evidence(limit, offset, task_id)


@router.get("/tasks/{task_id}/evidence")
async def task_evidence(
    task_id: UUID, queries: Queries, limit: Limit = 50, offset: Offset = 0
) -> Page[EvidenceView]:
    return await queries.evidence(limit, offset, task_id)


@router.get("/evidence/{evidence_id}")
async def evidence_detail(evidence_id: UUID, queries: Queries) -> EvidenceView:
    return EvidenceView.model_validate(await queries.get(Evidence, evidence_id))


@router.get("/tool-calls")
async def list_tool_calls(
    queries: Queries, limit: Limit = 50, offset: Offset = 0, task_id: UUID | None = None
) -> Page[ToolCallView]:
    return await queries.tool_calls(limit, offset, task_id)


@router.get("/tasks/{task_id}/tool-calls")
async def task_tool_calls(
    task_id: UUID, queries: Queries, limit: Limit = 50, offset: Offset = 0
) -> Page[ToolCallView]:
    return await queries.tool_calls(limit, offset, task_id)


@router.get("/tool-calls/{call_id}")
async def tool_call_detail(call_id: UUID, queries: Queries) -> ToolCallView:
    return ToolCallView.model_validate(await queries.tool_call(call_id))


@router.get("/tasks/{task_id}/status-history")
async def task_history(task_id: UUID, queries: Queries) -> list[HistoryView]:
    return [HistoryView.model_validate(row) for row in await queries.history(task_id)]


@router.get("/status-history/{history_id}")
async def history_detail(history_id: UUID, queries: Queries) -> HistoryView:
    return HistoryView.model_validate(await queries.get(TaskStatusHistory, history_id))


@router.get("/incidents")
async def list_incidents(
    queries: Queries, limit: Limit = 50, offset: Offset = 0, service_name: ServiceName = None
) -> Page[IncidentHit]:
    return await queries.incidents(limit, offset, service_name)


@router.get("/incidents/{incident_id}")
async def incident_detail(incident_id: UUID, queries: Queries) -> IncidentHit:
    return queries.incident_hit(await queries.incident(incident_id))


async def submit(gateway: ControlGateway, command: ControlCommand) -> ControlReceipt:
    try:
        return await gateway.submit(command)
    except (TimeoutError, RPCError, WorkflowFailureError, OSError):
        raise HTTPException(
            503, "Temporal 操作尚未确认，请以相同参数重试；已提交操作会保留并去重"
        ) from None


@router.post("/tasks/{task_id}/approval", status_code=202)
async def approve(
    task_id: UUID,
    body: ApprovalInput,
    principal: CurrentPrincipal,
    gateway: Gateway,
    csrf_token: Csrf,
) -> ControlReceipt:
    return await submit(
        gateway,
        ControlCommand(
            task_id=task_id,
            kind="approval",
            actor=principal.actor,
            payload=json_object(body.model_dump(mode="json")),
        ),
    )


@router.post("/tasks/{task_id}/judgment", status_code=202)
async def judge(
    task_id: UUID,
    body: AnswerInput,
    principal: CurrentPrincipal,
    gateway: Gateway,
    csrf_token: Csrf,
) -> ControlReceipt:
    return await submit(
        gateway,
        ControlCommand(
            task_id=task_id,
            kind="judgment",
            actor=principal.actor,
            payload=json_object(body.model_dump(mode="json")),
        ),
    )


@router.post("/tasks/{task_id}/information", status_code=202)
async def inform(
    task_id: UUID,
    body: AnswerInput,
    principal: CurrentPrincipal,
    gateway: Gateway,
    csrf_token: Csrf,
) -> ControlReceipt:
    return await submit(
        gateway,
        ControlCommand(
            task_id=task_id,
            kind="information",
            actor=principal.actor,
            payload=json_object(body.model_dump(mode="json")),
        ),
    )


@router.post("/tasks/{task_id}/takeover", status_code=202)
async def takeover(
    task_id: UUID,
    body: TakeoverInput,
    principal: CurrentPrincipal,
    gateway: Gateway,
    csrf_token: Csrf,
) -> ControlReceipt:
    return await submit(
        gateway,
        ControlCommand(
            task_id=task_id,
            kind="takeover",
            actor=principal.actor,
            payload=json_object(body.model_dump(mode="json")),
        ),
    )
