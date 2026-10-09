"""运维平台的 L0 高级 Tool；注册后仍只经统一 Dispatcher 调用。"""

from pydantic import model_validator

from app.connectors.ops_platform.client import OpsPlatformConnector, OpsPlatformResponseError
from app.connectors.ops_platform.models import (
    Application,
    Identifier,
    Owner,
    ServiceTreeNode,
    Ticket,
)
from app.policy.models import RiskLevel
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class ServiceQuery(ToolModel):
    service_name: Identifier


class ServiceContext(ToolModel):
    application: Application
    business_path: tuple[ServiceTreeNode, ...]
    owners: tuple[Owner, ...]


class ServiceListQuery(ToolModel):
    business_id: Identifier | None = None


class ServiceList(ToolModel):
    service_tree: tuple[ServiceTreeNode, ...]
    applications: tuple[Application, ...]


class TicketQuery(ToolModel):
    ticket_id: Identifier | None = None
    service_name: Identifier | None = None
    status: Identifier | None = None

    @model_validator(mode="after")
    def validate_query(self) -> "TicketQuery":
        if self.ticket_id is not None and (
            self.service_name is not None or self.status is not None
        ):
            raise ValueError("按工单 ID 读取时不能同时设置列表筛选条件")
        return self


class TicketList(ToolModel):
    tickets: tuple[Ticket, ...]


def register_ops_platform_tools(registry: ToolRegistry, connector: OpsPlatformConnector) -> None:
    async def get_service(query: ServiceQuery) -> ServiceContext:
        application = await connector.get_application(query.service_name)
        nodes = {node.id: node for node in await connector.list_service_tree()}
        path: list[ServiceTreeNode] = []
        visited: set[str] = set()
        current: str | None = application.business_id
        while current is not None:
            if current not in nodes or current in visited:
                raise OpsPlatformResponseError("运维平台服务树的业务关系缺失或存在环路")
            visited.add(current)
            node = nodes[current]
            path.append(node)
            current = node.parent_id
        owners = await connector.list_owners(query.service_name)
        if {owner.id for owner in owners} != set(application.owner_ids):
            raise OpsPlatformResponseError("运维平台负责人与应用引用不一致")
        return ServiceContext(
            application=application, business_path=tuple(reversed(path)), owners=owners
        )

    async def list_services(query: ServiceListQuery) -> ServiceList:
        return ServiceList(
            service_tree=await connector.list_service_tree(),
            applications=await connector.list_applications(business_id=query.business_id),
        )

    async def query_tickets(query: TicketQuery) -> TicketList:
        tickets = (
            (await connector.get_ticket(query.ticket_id),)
            if query.ticket_id is not None
            else await connector.list_tickets(service_name=query.service_name, status=query.status)
        )
        return TicketList(tickets=tickets)

    registry.register(
        name="get_ops_service",
        description="读取运维平台的服务、业务归属与负责人",
        input_model=ServiceQuery,
        output_model=ServiceContext,
        handler=get_service,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="list_ops_services",
        description="读取运维平台服务树与应用清单",
        input_model=ServiceListQuery,
        output_model=ServiceList,
        handler=list_services,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="query_ops_tickets",
        description="按服务/状态查询工单或按工单 ID 读取详情",
        input_model=TicketQuery,
        output_model=TicketList,
        handler=query_tickets,
        risk_level=RiskLevel.L0,
    )
