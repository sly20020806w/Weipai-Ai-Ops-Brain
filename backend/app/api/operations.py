"""认知与运营 HTTP 适配；业务查询、编辑与指标均复用服务层。"""

from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import AwareDatetime, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CurrentPrincipal
from app.api.console import Csrf, Limit, Offset, ServiceName
from app.api.dependencies import get_session
from app.config import Settings
from app.db.base import utc_now
from app.graph.changes.models import ChangeEvent
from app.graph.changes.schemas import ChangeKind, ChangeSource
from app.graph.models import GraphEdge, GraphNode
from app.knowledge.models import KnowledgeEntry
from app.knowledge.schemas import KnowledgeType, KnowledgeView
from app.knowledge.service import view as knowledge_view
from app.learning.evaluation.metrics import MetricsReport, MetricValue
from app.runbooks.models import Runbook
from app.runbooks.schemas import RunbookMaturity, RunbookView
from app.runbooks.service import view as runbook_view
from app.tasks.catalog_service import CatalogService
from app.tasks.console_models import Page
from app.tasks.inspection.models import RiskEntry
from app.tasks.operations_models import (
    AuditKind,
    AuditView,
    Center,
    ChangeEntryView,
    KnowledgeInput,
    RiskView,
    RunbookInput,
    ScenarioDetail,
    ScenarioView,
    TimeWindow,
)
from app.tasks.operations_queries import OperationsQueries, edge_view
from app.tasks.states import TaskStatus
from app.tools.graph import EdgeView, GraphContext, NodeView

router = APIRouter(
    prefix="/api",
    tags=["认知与运营"],
    responses={
        401: {"description": "请先登录"},
        403: {"description": "CSRF 或来源校验失败"},
        404: {"description": "记录不存在"},
        409: {"description": "编辑内容冲突"},
        503: {"description": "存储或 AI 网关暂时不可用"},
    },
)


def get_operations(session: Annotated[AsyncSession, Depends(get_session)]) -> OperationsQueries:
    return OperationsQueries(session)


def get_catalog(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> CatalogService:
    settings: Settings = request.app.state.settings
    return CatalogService(session, settings)


Queries = Annotated[OperationsQueries, Depends(get_operations)]
Catalog = Annotated[CatalogService, Depends(get_catalog)]


def get_window(
    start: Annotated[AwareDatetime | None, Query()] = None,
    end: Annotated[AwareDatetime | None, Query()] = None,
) -> TimeWindow:
    try:
        return TimeWindow(start=start or utc_now() - timedelta(days=30), end=end or utc_now())
    except ValidationError:
        raise HTTPException(422, "开始时间必须早于结束时间，时间必须带时区") from None


Window = Annotated[TimeWindow, Depends(get_window)]
Hops = Annotated[int, Query(ge=1, le=4)]
ServicePath = Annotated[str, Path(min_length=1, max_length=300)]


@router.get("/services")
async def services(queries: Queries, limit: Limit = 50, offset: Offset = 0) -> Page[NodeView]:
    return await queries.nodes(limit, offset, "service")


@router.get("/services/{service_name}")
async def service(service_name: ServicePath, queries: Queries, hops: Hops = 2) -> GraphContext:
    return await queries.graph(service_name, hops)


@router.get("/services/{service_name}/dependencies")
async def dependencies(
    service_name: ServicePath,
    queries: Queries,
    hops: Hops = 1,
    direction: Literal["upstream", "downstream", "both"] = "both",
) -> GraphContext:
    return await queries.graph(service_name, hops, direction)


@router.get("/context-graph/nodes")
async def nodes(
    queries: Queries,
    limit: Limit = 50,
    offset: Offset = 0,
    kind: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> Page[NodeView]:
    return await queries.nodes(limit, offset, kind)


@router.get("/context-graph/nodes/{node_id}")
async def node(node_id: UUID, queries: Queries) -> NodeView:
    return NodeView.model_validate(await queries.get(GraphNode, node_id), from_attributes=True)


@router.get("/context-graph/edges")
async def edges(
    queries: Queries, limit: Limit = 50, offset: Offset = 0, node_id: UUID | None = None
) -> Page[EdgeView]:
    return await queries.edges(limit, offset, node_id)


@router.get("/context-graph/edges/{edge_id}")
async def edge(edge_id: UUID, queries: Queries) -> EdgeView:
    return edge_view(await queries.get(GraphEdge, edge_id), utc_now())


@router.get("/changes")
async def changes(
    queries: Queries,
    window: Window,
    limit: Limit = 50,
    offset: Offset = 0,
    service_name: ServiceName = None,
    kind: ChangeKind | None = None,
    source: ChangeSource | None = None,
) -> Page[ChangeEntryView]:
    return await queries.changes(limit, offset, service_name, window, kind, source)


@router.get("/changes/{change_id}")
async def change(change_id: UUID, queries: Queries) -> ChangeEntryView:
    return ChangeEntryView.model_validate(await queries.get(ChangeEvent, change_id))


@router.get("/runbooks")
async def runbooks(
    queries: Queries, limit: Limit = 50, offset: Offset = 0, maturity: RunbookMaturity | None = None
) -> Page[RunbookView]:
    return await queries.runbooks(limit, offset, maturity)


@router.get("/runbooks/{runbook_id}")
async def runbook(runbook_id: UUID, queries: Queries) -> RunbookView:
    return runbook_view(await queries.get(Runbook, runbook_id))


@router.post("/runbooks", status_code=201)
async def create_runbook(
    body: RunbookInput, catalog: Catalog, principal: CurrentPrincipal, csrf_token: Csrf
) -> RunbookView:
    result = await catalog.runbook("create", body, None, principal.actor)
    assert result is not None
    return result


@router.put("/runbooks/{runbook_id}")
async def update_runbook(
    runbook_id: UUID,
    body: RunbookInput,
    catalog: Catalog,
    principal: CurrentPrincipal,
    csrf_token: Csrf,
) -> RunbookView:
    result = await catalog.runbook("update", body, runbook_id, principal.actor)
    assert result is not None
    return result


@router.delete("/runbooks/{runbook_id}", status_code=204)
async def delete_runbook(
    runbook_id: UUID, catalog: Catalog, principal: CurrentPrincipal, csrf_token: Csrf
) -> Response:
    await catalog.runbook("delete", None, runbook_id, principal.actor)
    return Response(status_code=204)


@router.get("/knowledge")
async def knowledge(
    queries: Queries, limit: Limit = 50, offset: Offset = 0, kind: KnowledgeType | None = None
) -> Page[KnowledgeView]:
    return await queries.knowledge(limit, offset, kind)


@router.get("/knowledge/{entry_id}")
async def knowledge_entry(entry_id: UUID, queries: Queries) -> KnowledgeView:
    return knowledge_view(await queries.get(KnowledgeEntry, entry_id))


@router.post("/knowledge", status_code=201)
async def create_knowledge(
    body: KnowledgeInput, catalog: Catalog, principal: CurrentPrincipal, csrf_token: Csrf
) -> KnowledgeView:
    result = await catalog.knowledge("create", body, None, principal.actor)
    assert result is not None
    return result


@router.put("/knowledge/{entry_id}")
async def update_knowledge(
    entry_id: UUID,
    body: KnowledgeInput,
    catalog: Catalog,
    principal: CurrentPrincipal,
    csrf_token: Csrf,
) -> KnowledgeView:
    result = await catalog.knowledge("update", body, entry_id, principal.actor)
    assert result is not None
    return result


@router.delete("/knowledge/{entry_id}", status_code=204)
async def delete_knowledge(
    entry_id: UUID, catalog: Catalog, principal: CurrentPrincipal, csrf_token: Csrf
) -> Response:
    await catalog.knowledge("delete", None, entry_id, principal.actor)
    return Response(status_code=204)


def scenario_routes(center: Center) -> None:
    async def listing(
        queries: Queries,
        limit: Limit = 50,
        offset: Offset = 0,
        service_name: ServiceName = None,
        status: TaskStatus | None = None,
    ) -> Page[ScenarioView]:
        return await queries.scenarios(center, limit, offset, service_name, status)

    async def detail(task_id: UUID, queries: Queries) -> ScenarioDetail:
        return await queries.scenario(center, task_id)

    router.add_api_route(
        f"/{center}",
        listing,
        methods=["GET"],
        response_model=Page[ScenarioView],
        operation_id=f"list_{center.replace('-', '_')}",
    )
    router.add_api_route(
        f"/{center}/{{task_id}}",
        detail,
        methods=["GET"],
        response_model=ScenarioDetail,
        operation_id=f"get_{center.replace('-', '_')}",
    )


for center in (
    "releases",
    "tickets",
    "inspections",
    "war-rooms",
    "architecture-reviews",
    "automations",
):
    scenario_routes(center)


@router.get("/risks")
async def risks(
    queries: Queries,
    limit: Limit = 50,
    offset: Offset = 0,
    service_name: ServiceName = None,
    active: bool | None = None,
    category: Literal["stability", "capacity", "security", "cost"] | None = None,
) -> Page[RiskView]:
    return await queries.risks(limit, offset, service_name, active, category)


@router.get("/risks/{risk_id}")
async def risk(risk_id: UUID, queries: Queries) -> RiskView:
    return RiskView.model_validate(await queries.get(RiskEntry, risk_id))


@router.get("/metrics")
async def metrics(queries: Queries, window: Window) -> MetricsReport:
    return await queries.metrics(window)


@router.get("/metrics/{metric_name}")
async def metric(metric_name: str, queries: Queries, window: Window) -> MetricValue:
    return await queries.metric(metric_name, window)


@router.get("/audits")
async def audits(
    queries: Queries,
    window: Window,
    limit: Limit = 50,
    offset: Offset = 0,
    actor: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    event_type: AuditKind | None = None,
    task_id: UUID | None = None,
) -> Page[AuditView]:
    return await queries.audits(limit, offset, window, actor, event_type, task_id)


@router.get("/audits/{audit_id}")
async def audit(audit_id: UUID, queries: Queries) -> AuditView:
    return await queries.audit(audit_id)
