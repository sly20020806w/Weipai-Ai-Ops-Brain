"""控制台只读投影：查询本地已保存的任务、事件、证据与调用历史。"""

import json
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.learning.models import IncidentHit, IncidentReport
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.tasks.approval.models import ApprovalTicket
from app.tasks.console_models import (
    ConsoleModel,
    EventView,
    EvidenceView,
    InteractionView,
    Page,
    TaskView,
    ToolCallView,
)
from app.tasks.human.models import question_id
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.workflow_models import ApprovalPrompt, HumanPrompt, HumanWaitRequest, TaskSnapshot
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventOrigin


class ConsoleNotFound(LookupError):
    pass


class ConsoleConflict(ValueError):
    pass


async def page[T, V: ConsoleModel](
    session: AsyncSession, statement: Select[tuple[T]], limit: int, offset: int, view: type[V]
) -> Page[V]:
    total = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )
    rows = await session.scalars(statement.limit(limit).offset(offset))
    return Page(
        items=[view.model_validate(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


class ConsoleQueries:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get[T: Base](self, model: type[T], identity: UUID) -> T:
        record = await self.session.get(model, identity)
        if record is None:
            raise ConsoleNotFound("记录不存在")
        return record

    async def task(self, identity: UUID) -> AITask:
        return await self.get(AITask, identity)

    async def approval(self, task_id: UUID, version: int) -> tuple[ApprovalTicket, ApprovalPrompt]:
        await self.task(task_id)
        record = await self.session.scalar(
            select(Evidence).where(
                Evidence.task_id == task_id,
                Evidence.source_tool == "approval.request",
                Evidence.parameters["task"]["version"].as_integer() == version,
            )
        )
        if record is None:
            raise ConsoleNotFound("审批单不存在")
        ticket = ApprovalTicket.model_validate_json(json.dumps(record.result_snapshot))
        prompt = ApprovalPrompt(
            TaskSnapshot(str(task_id), TaskStatus.WAITING_APPROVAL, version),
            str(ticket.approval_id),
            str(record.id),
            str(ticket.plan_evidence_id),
            ticket.action_hash,
        )
        return ticket, prompt

    async def question(self, task_id: UUID, version: int) -> HumanPrompt:
        await self.task(task_id)
        record = await self.session.scalar(
            select(Evidence).where(
                Evidence.task_id == task_id,
                Evidence.source_tool == "human.question",
                Evidence.parameters["task"]["version"].as_integer() == version,
            )
        )
        if record is None:
            raise ConsoleNotFound("人工问题不存在")
        request = TypeAdapter(HumanWaitRequest).validate_python(record.parameters)
        return HumanPrompt(
            request.task,
            question_id(request.task),
            str(record.id),
            request.question,
            request.resume_status,
        )

    async def interaction(self, task_id: UUID) -> InteractionView:
        task = await self.task(task_id)
        result = InteractionView(
            task_id=task.id, status=task.status, status_version=task.status_version
        )
        try:
            if task.status is TaskStatus.WAITING_APPROVAL:
                result.approval, prompt = await self.approval(task.id, task.status_version)
                result.approval_request_evidence_id = UUID(prompt.request_evidence_id)
            elif task.status in {TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION}:
                result.question = await self.question(task.id, task.status_version)
        except ConsoleNotFound:
            # 状态提交与通知 Activity 之间允许短暂没有问题/审批单；不得伪造授权。
            if task.status in {TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION}:
                result.recovery = await self.recovery(task.id, task.status_version)
                if result.recovery:
                    result.recovery_question_id = UUID(question_id(result.recovery.task))
        return result

    async def recovery(self, task_id: UUID, version: int) -> HumanWaitRequest | None:
        task = await self.task(task_id)
        if (
            task.status not in {TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION}
            or task.status_version != version
        ):
            return None
        history = await self.session.scalar(
            select(TaskStatusHistory).where(
                TaskStatusHistory.task_id == task_id, TaskStatusHistory.sequence == version
            )
        )
        if history is None or history.from_status not in {
            TaskStatus.CONTEXT_BUILDING,
            TaskStatus.RUNBOOK_MATCHING,
            TaskStatus.INVESTIGATING,
            TaskStatus.RCA,
            TaskStatus.PLANNING,
        }:
            return None
        return HumanWaitRequest(
            TaskSnapshot(str(task.id), task.status, task.status_version),
            f"请确认恢复条件并补充信息：{history.reason[:1000]}",
            history.from_status,
        )

    async def tool_call(self, identity: UUID) -> AuditRecord:
        record = await self.get(AuditRecord, identity)
        if record.event_type is not AuditEventType.TOOL_CALL:
            raise ConsoleNotFound("Tool 调用不存在")
        return record

    async def incident(self, identity: UUID) -> Evidence:
        record = await self.get(Evidence, identity)
        if record.source_tool != "postmortem":
            raise ConsoleNotFound("事故复盘不存在")
        return record

    async def history(self, task_id: UUID) -> list[TaskStatusHistory]:
        await self.task(task_id)
        return list(
            await self.session.scalars(
                select(TaskStatusHistory)
                .where(TaskStatusHistory.task_id == task_id)
                .order_by(TaskStatusHistory.sequence)
            )
        )

    async def event(self, identity: UUID) -> OpsEvent:
        return await self.get(OpsEvent, identity)

    async def tasks(
        self, limit: int, offset: int, status: TaskStatus | None, source: TaskSource | None
    ) -> Page[TaskView]:
        statement = select(AITask)
        if status is not None:
            statement = statement.where(AITask._status == status)
        if source is not None:
            statement = statement.where(AITask.source == source)
        return await page(
            self.session,
            statement.order_by(AITask.created_at.desc(), AITask.id),
            limit,
            offset,
            TaskView,
        )

    async def events(
        self,
        limit: int,
        offset: int,
        source: TaskSource | None,
        origin: EventOrigin | None,
        service_name: str | None,
    ) -> Page[EventView]:
        statement = select(OpsEvent)
        if source is not None:
            statement = statement.where(OpsEvent.source == source.value)
        if origin is not None:
            statement = statement.where(OpsEvent.origin == origin)
        if service_name is not None:
            statement = statement.where(OpsEvent.service_name == service_name)
        return await page(
            self.session,
            statement.order_by(OpsEvent.occurred_at.desc(), OpsEvent.id),
            limit,
            offset,
            EventView,
        )

    async def evidence(self, limit: int, offset: int, task_id: UUID | None) -> Page[EvidenceView]:
        statement = select(Evidence)
        if task_id is not None:
            await self.task(task_id)
            statement = statement.where(Evidence.task_id == task_id)
        return await page(
            self.session,
            statement.order_by(Evidence.collected_at, Evidence.id),
            limit,
            offset,
            EvidenceView,
        )

    async def tool_calls(self, limit: int, offset: int, task_id: UUID | None) -> Page[ToolCallView]:
        statement = select(AuditRecord).where(AuditRecord.event_type == AuditEventType.TOOL_CALL)
        if task_id is not None:
            await self.task(task_id)
            statement = statement.where(AuditRecord.task_id == task_id)
        return await page(
            self.session,
            statement.order_by(AuditRecord.occurred_at, AuditRecord.id),
            limit,
            offset,
            ToolCallView,
        )

    async def incidents(
        self, limit: int, offset: int, service_name: str | None
    ) -> Page[IncidentHit]:
        statement = select(Evidence).where(Evidence.source_tool == "postmortem")
        if service_name is not None:
            statement = statement.where(
                Evidence.result_snapshot["service_name"].as_string() == service_name
            )
        total = await self.session.scalar(select(func.count()).select_from(statement.subquery()))
        records = await self.session.scalars(
            statement.order_by(Evidence.collected_at.desc(), Evidence.id)
            .limit(limit)
            .offset(offset)
        )
        return Page(
            items=[self.incident_hit(e) for e in records],
            total=total or 0,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def incident_hit(record: Evidence) -> IncidentHit:
        return IncidentHit(
            evidence_id=record.id,
            report=IncidentReport.model_validate_json(json.dumps(record.result_snapshot)),
        )
