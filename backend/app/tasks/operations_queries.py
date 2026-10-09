"""认知与运营只读投影；只查本地关联、引用和证据，不触达外部系统。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, String, Uuid, cast, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.graph.changes.models import ChangeEvent
from app.graph.models import GraphEdge, GraphNode
from app.graph.service import GraphService
from app.knowledge.models import KnowledgeEntry
from app.knowledge.schemas import KnowledgeType, KnowledgeView
from app.knowledge.service import view as knowledge_view
from app.learning.evaluation.metrics import (
    EvaluationService,
    EvaluationWindow,
    MetricsReport,
    MetricValue,
)
from app.ledger.models import AuditRecord, CatalogAudit, Evidence
from app.runbooks.models import Runbook
from app.runbooks.schemas import RunbookMaturity, RunbookView
from app.runbooks.service import view as runbook_view
from app.tasks.console_models import EventView, EvidenceView, Page, TaskView
from app.tasks.console_queries import ConsoleNotFound, ConsoleQueries
from app.tasks.inspection.models import RiskEntry
from app.tasks.models import AITask
from app.tasks.operations_models import (
    AuditKind,
    AuditView,
    Center,
    ChangeEntryView,
    RiskView,
    ScenarioDetail,
    ScenarioView,
    TimeWindow,
)
from app.tasks.states import TaskSource, TaskStatus
from app.tools.graph import EdgeView, GraphContext, GraphQueries, NodeView
from app.triggers.models import OpsEvent


async def project_page[T, V: BaseModel](
    session: AsyncSession,
    statement: Select[tuple[T]],
    limit: int,
    offset: int,
    convert: Callable[[T], V],
) -> Page[V]:
    total = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )
    rows = await session.scalars(statement.limit(limit).offset(offset))
    return Page(items=[convert(row) for row in rows], total=total or 0, limit=limit, offset=offset)


def center_filter(center: Center) -> ColumnElement[bool]:
    match center:
        case "releases":
            return OpsEvent.source == TaskSource.RELEASE.value
        case "tickets":
            return OpsEvent.source == TaskSource.TICKET.value
        case "inspections":
            return (
                (OpsEvent.origin == "schedule")
                & (OpsEvent.source == TaskSource.SCHEDULE.value)
                & (
                    OpsEvent.external_id.startswith("workday-inspection:")
                    | OpsEvent.external_id.startswith("hourly-capacity:")
                    | OpsEvent.external_id.startswith("daily-governance:")
                )
            )
        case "war-rooms" | "architecture-reviews":
            prefix = "war-room:" if center == "war-rooms" else "architecture-review:"
            return (
                (OpsEvent.origin == "manual")
                & (OpsEvent.source == TaskSource.HUMAN.value)
                & OpsEvent.external_id.startswith(prefix)
            )
        case "automations":
            return (
                (OpsEvent.origin == "learning")
                & (OpsEvent.source == TaskSource.AI.value)
                & OpsEvent.external_id.startswith("automation:")
            )


def edge_view(edge: GraphEdge, at: datetime) -> EdgeView:
    return EdgeView(
        id=edge.id,
        from_node_id=edge.from_node_id,
        to_node_id=edge.to_node_id,
        relation=edge.relation,
        source=edge.source,
        confidence=edge.confidence,
        first_seen=edge.first_seen,
        last_seen=edge.last_seen,
        freshness_seconds=edge.freshness_at(at).total_seconds(),
    )


class OperationsQueries(ConsoleQueries):
    async def metrics(self, window: TimeWindow) -> MetricsReport:
        return await EvaluationService(self.session).report(
            EvaluationWindow.model_validate_json(window.model_dump_json())
        )

    async def metric(self, name: str, window: TimeWindow) -> MetricValue:
        report = await self.metrics(window)
        value = next((v for v in report.metrics if v.name == name), None)
        if value is None:
            raise ConsoleNotFound("能力指标不存在")
        return value

    async def nodes(self, limit: int, offset: int, kind: str | None = None) -> Page[NodeView]:
        statement = select(GraphNode)
        if kind is not None:
            statement = statement.where(GraphNode.kind == kind)
        return await project_page(
            self.session,
            statement.order_by(GraphNode.kind, GraphNode.external_id, GraphNode.id),
            limit,
            offset,
            lambda n: NodeView.model_validate(n, from_attributes=True),
        )

    async def graph(
        self, service_name: str, hops: int, direction: str | None = None
    ) -> GraphContext:
        return await GraphQueries(GraphService(self.session)).read(
            service_name, hops, direction or "both", dependencies=direction is not None
        )

    async def edges(self, limit: int, offset: int, node_id: UUID | None) -> Page[EdgeView]:
        from app.db.base import utc_now

        statement = select(GraphEdge)
        if node_id is not None:
            await self.get(GraphNode, node_id)
            statement = statement.where(
                (GraphEdge.from_node_id == node_id) | (GraphEdge.to_node_id == node_id)
            )
        at = utc_now()
        return await project_page(
            self.session,
            statement.order_by(GraphEdge.last_seen.desc(), GraphEdge.id),
            limit,
            offset,
            lambda e: edge_view(e, at),
        )

    async def changes(
        self,
        limit: int,
        offset: int,
        service_name: str | None,
        window: TimeWindow,
        kind: str | None,
        source: str | None,
    ) -> Page[ChangeEntryView]:
        statement = select(ChangeEvent).where(
            ChangeEvent.occurred_at >= window.start, ChangeEvent.occurred_at < window.end
        )
        for column, value in (
            (ChangeEvent.service_name, service_name),
            (ChangeEvent.kind, kind),
            (ChangeEvent.source, source),
        ):
            if value is not None:
                statement = statement.where(column == value)
        return await project_page(
            self.session,
            statement.order_by(ChangeEvent.occurred_at, ChangeEvent.id),
            limit,
            offset,
            ChangeEntryView.model_validate,
        )

    async def knowledge(
        self, limit: int, offset: int, kind: KnowledgeType | None
    ) -> Page[KnowledgeView]:
        statement = select(KnowledgeEntry)
        if kind is not None:
            statement = statement.where(KnowledgeEntry.kind == kind.value)
        return await project_page(
            self.session,
            statement.order_by(KnowledgeEntry.created_at.desc(), KnowledgeEntry.id),
            limit,
            offset,
            knowledge_view,
        )

    async def runbooks(
        self, limit: int, offset: int, maturity: RunbookMaturity | None
    ) -> Page[RunbookView]:
        statement = select(Runbook)
        if maturity is not None:
            statement = statement.where(Runbook.maturity == maturity.value)
        return await project_page(
            self.session,
            statement.order_by(Runbook.created_at.desc(), Runbook.id),
            limit,
            offset,
            runbook_view,
        )

    async def scenarios(
        self,
        center: Center,
        limit: int,
        offset: int,
        service_name: str | None,
        status: TaskStatus | None,
    ) -> Page[ScenarioView]:
        statement = (
            select(OpsEvent, AITask)
            .join(AITask, AITask.id == OpsEvent.task_id)
            .where(center_filter(center))
        )
        if service_name is not None:
            statement = statement.where(OpsEvent.service_name == service_name)
        if status is not None:
            statement = statement.where(AITask._status == status)
        total = await self.session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self.session.execute(
            statement.order_by(OpsEvent.occurred_at.desc(), OpsEvent.id).limit(limit).offset(offset)
        )
        return Page(
            items=[
                ScenarioView(task=TaskView.model_validate(t), event=EventView.model_validate(e))
                for e, t in rows
            ],
            total=total or 0,
            limit=limit,
            offset=offset,
        )

    async def scenario(self, center: Center, task_id: UUID) -> ScenarioDetail:
        event = await self.session.scalar(
            select(OpsEvent).where(OpsEvent.task_id == task_id, center_filter(center))
        )
        if event is None:
            raise ConsoleNotFound("该运营场景的任务不存在")
        evidence = await self.session.scalars(
            select(Evidence)
            .where(Evidence.task_id == task_id)
            .order_by(Evidence.collected_at, Evidence.id)
        )
        return ScenarioDetail(
            task=TaskView.model_validate(await self.task(task_id)),
            event=EventView.model_validate(event),
            evidence=[EvidenceView.model_validate(e) for e in evidence],
        )

    async def risks(
        self,
        limit: int,
        offset: int,
        service_name: str | None,
        active: bool | None,
        category: Literal["stability", "capacity", "security", "cost"] | None,
    ) -> Page[RiskView]:
        statement = select(RiskEntry)
        if service_name is not None:
            statement = statement.where(RiskEntry.service_name == service_name)
        if active is not None:
            statement = statement.where(RiskEntry.active == active)
        if category is not None:
            statement = statement.where(RiskEntry.category == category)
        return await project_page(
            self.session,
            statement.order_by(RiskEntry.last_seen.desc(), RiskEntry.id),
            limit,
            offset,
            RiskView.model_validate,
        )

    async def audits(
        self,
        limit: int,
        offset: int,
        window: TimeWindow,
        actor: str | None,
        event_type: AuditKind | None,
        task_id: UUID | None,
    ) -> Page[AuditView]:
        if task_id is not None:
            await self.task(task_id)
        # 合并两个只追加来源后再分页，total 与筛选、排序作用域保持一致。
        combined = union_all(
            select(
                AuditRecord.id,
                AuditRecord.task_id,
                cast(AuditRecord.event_type, String).label("event_type"),
                AuditRecord.actor,
                AuditRecord.operation,
                AuditRecord.outcome,
                AuditRecord.evidence_id,
                AuditRecord.details,
                AuditRecord.occurred_at,
            ),
            select(
                CatalogAudit.id,
                literal(None, type_=Uuid).label("task_id"),
                literal("catalog_edit"),
                CatalogAudit.actor,
                CatalogAudit.operation,
                literal("recorded"),
                literal(None, type_=Uuid).label("evidence_id"),
                CatalogAudit.details,
                CatalogAudit.occurred_at,
            ),
        ).subquery()
        statement = select(combined).where(
            combined.c.occurred_at >= window.start, combined.c.occurred_at < window.end
        )
        for column, value in (
            (combined.c.actor, actor),
            (combined.c.event_type, event_type),
            (combined.c.task_id, task_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        total = await self.session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self.session.execute(
            statement.order_by(combined.c.occurred_at.desc(), combined.c.id)
            .limit(limit)
            .offset(offset)
        )
        return Page(
            items=[AuditView.model_validate(r) for r in rows.mappings()],
            total=total or 0,
            limit=limit,
            offset=offset,
        )

    async def audit(self, identity: UUID) -> AuditView:
        record: AuditRecord | CatalogAudit | None = await self.session.get(AuditRecord, identity)
        if record is None:
            record = await self.session.get(CatalogAudit, identity)
        if record is None:
            raise ConsoleNotFound("审计记录不存在")
        return AuditView.model_validate(record)
