"""可注入快照的离线样例；数据不代表微派真实环境。"""

from datetime import UTC, datetime

from app.connectors.ops_platform.client import (
    OpsPlatformConnector,
    OpsPlatformError,
    OpsPlatformNotFound,
    checked_identifier,
)
from app.connectors.ops_platform.models import (
    Application,
    OpsPlatformSnapshot,
    Owner,
    ServiceTreeNode,
    Ticket,
)


def sample_snapshot() -> OpsPlatformSnapshot:
    return OpsPlatformSnapshot(
        service_tree=(
            ServiceTreeNode(id="weipai", name="微派（样例）"),
            ServiceTreeNode(id="payment", name="支付业务（样例）", parent_id="weipai"),
            ServiceTreeNode(id="commerce", name="交易业务（样例）", parent_id="weipai"),
        ),
        applications=(
            Application(
                id="app-payment",
                service_name="payment-service",
                name="支付服务（样例）",
                business_id="payment",
                owner_ids=("owner-payment",),
            ),
            Application(
                id="app-checkout",
                service_name="checkout-service",
                name="交易服务（样例）",
                business_id="commerce",
                owner_ids=("owner-checkout",),
            ),
        ),
        owners=(
            Owner(id="owner-payment", name="支付负责人（样例）", team="支付团队（样例）"),
            Owner(id="owner-checkout", name="交易负责人（样例）", team="交易团队（样例）"),
        ),
        tickets=(
            Ticket(
                id="TICKET-1001",
                title="payment-service 发布后错误率升高（样例）",
                description="样例工单：请调查 payment-service v2.3.7 发布后的 5xx。",
                service_name="payment-service",
                status="open",
                requester_id="requester-sample",
                assignee_id="owner-payment",
                created_at=datetime(2026, 10, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 10, 1, 1, 5, tzinfo=UTC),
            ),
            Ticket(
                id="TICKET-1002",
                title="交易服务巡检（样例）",
                description="样例工单：巡检已结束。",
                service_name="checkout-service",
                status="closed",
                requester_id="requester-sample",
                assignee_id="owner-checkout",
                created_at=datetime(2026, 10, 1, 0, tzinfo=UTC),
                updated_at=datetime(2026, 10, 1, 0, 30, tzinfo=UTC),
            ),
        ),
    )


class FakeOpsPlatformConnector(OpsPlatformConnector):
    def __init__(self, snapshot: OpsPlatformSnapshot | None = None) -> None:
        super().__init__()
        self._snapshot = OpsPlatformSnapshot.model_validate(
            snapshot if snapshot is not None else sample_snapshot()
        )
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise OpsPlatformError("运维平台 Connector 已关闭")

    async def list_service_tree(self) -> tuple[ServiceTreeNode, ...]:
        self._ensure_open()
        return self._snapshot.service_tree

    async def list_applications(self, *, business_id: str | None = None) -> tuple[Application, ...]:
        self._ensure_open()
        if business_id is not None:
            checked_identifier(business_id)
        return tuple(
            app
            for app in self._snapshot.applications
            if business_id is None or app.business_id == business_id
        )

    async def get_application(self, service_name: str) -> Application:
        self._ensure_open()
        checked_identifier(service_name)
        for app in self._snapshot.applications:
            if app.service_name == service_name:
                return app
        raise OpsPlatformNotFound("运维平台应用不存在")

    async def list_owners(self, service_name: str) -> tuple[Owner, ...]:
        self._ensure_open()
        checked_identifier(service_name)
        ids = {
            owner_id
            for app in self._snapshot.applications
            if app.service_name == service_name
            for owner_id in app.owner_ids
        }
        return tuple(owner for owner in self._snapshot.owners if owner.id in ids)

    async def list_tickets(
        self, *, service_name: str | None = None, status: str | None = None
    ) -> tuple[Ticket, ...]:
        self._ensure_open()
        if service_name is not None:
            checked_identifier(service_name)
        if status is not None:
            checked_identifier(status)
        return tuple(
            ticket
            for ticket in self._snapshot.tickets
            if (service_name is None or ticket.service_name == service_name)
            and (status is None or ticket.status == status)
        )

    async def get_ticket(self, ticket_id: str) -> Ticket:
        self._ensure_open()
        checked_identifier(ticket_id)
        for ticket in self._snapshot.tickets:
            if ticket.id == ticket_id:
                return ticket
        raise OpsPlatformNotFound("运维平台工单不存在")
