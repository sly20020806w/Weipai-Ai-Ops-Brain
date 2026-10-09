"""工单事实与动作端；Reader 无写方法，写端仅接受短时精确动作凭证。"""

import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2 as httpx
from pydantic import SecretStr

from app.connectors.kubernetes.execution import ActionCredential
from app.connectors.models import (
    ExecutorCredentials,
    ReaderCredentials,
    validate_credential_separation,
)
from app.connectors.ops_platform.client import HTTPOpsPlatformConnector, OpsPlatformResponseError
from app.connectors.ops_platform.config import OpsPlatformConfig
from app.connectors.ops_platform.fake import FakeOpsPlatformConnector, sample_snapshot
from app.connectors.ops_platform.models import PermissionRequest, Ticket
from app.db.base import utc_now
from app.executor.ticket_models import (
    PermissionGrant,
    PermissionQuery,
    PermissionState,
    TicketCommand,
    TicketReceipt,
    ticket_command_hash,
)


class TicketState:
    """模拟源平台存储，可由重启后的 Worker 共享；不属于本平台数据库。"""

    def __init__(self) -> None:
        self.tickets = {t.id: t for t in sample_snapshot().tickets}
        now = utc_now()
        for ticket_id, complete in (("TICKET-PERMISSION", True), ("TICKET-INCOMPLETE", False)):
            self.tickets[ticket_id] = Ticket(
                id=ticket_id,
                title="支付日志只读权限申请（样例）",
                description="排查支付工单，需要一天只读日志权限。",
                service_name="payment-service",
                status="open",
                requester_id="requester-sample",
                assignee_id="owner-payment",
                created_at=now,
                updated_at=now,
                permission_request=PermissionRequest(
                    subject_id="user-sample",
                    resource="payment-logs",
                    permission="read",
                    reason="排查支付工单",
                    expires_at=now + timedelta(hours=24),
                )
                if complete
                else PermissionRequest(subject_id="user-sample"),
            )
        self.grants: dict[tuple[str, str, str], PermissionGrant] = {}
        self.receipts: dict[UUID, TicketReceipt] = {}
        self.credentials: dict[str, ActionCredential] = {}
        self.execution_count = 0
        self.issue_count = 0


class FakeTicketReader(FakeOpsPlatformConnector):
    def __init__(self, state: TicketState) -> None:
        super().__init__()
        self.state = state

    async def get_ticket(self, ticket_id: str) -> Ticket:
        self._ensure_open()
        if ticket_id not in self.state.tickets:
            return await super().get_ticket(ticket_id)
        return self.state.tickets[ticket_id].model_copy(deep=True)

    async def list_tickets(
        self, *, service_name: str | None = None, status: str | None = None
    ) -> tuple[Ticket, ...]:
        self._ensure_open()
        return tuple(
            t.model_copy(deep=True)
            for t in self.state.tickets.values()
            if (service_name is None or t.service_name == service_name)
            and (status is None or t.status == status)
        )

    async def get_permission(self, query: PermissionQuery) -> PermissionState:
        self._ensure_open()
        return PermissionState(
            **query.model_dump(),
            grant=self.state.grants.get((query.service_name, query.subject_id, query.resource)),
        )


class HTTPTicketReader(HTTPOpsPlatformConnector):
    async def get_permission(self, query: PermissionQuery) -> PermissionState:
        response = await self._http.get("api/permissions", params=query.model_dump())
        if not response.is_success:
            raise OpsPlatformResponseError("权限事实读取失败")
        value = PermissionState.model_validate_json(response.content)
        if any(getattr(value, field) != getattr(query, field) for field in query.model_fields):
            raise OpsPlatformResponseError("权限事实目标不匹配")
        return value


TicketReader = FakeTicketReader | HTTPTicketReader


class TicketWriteConnector(ABC):
    @abstractmethod
    async def issue(self, command: TicketCommand, ttl_seconds: int) -> ActionCredential: ...

    @abstractmethod
    async def execute(
        self, command: TicketCommand, credential: ActionCredential
    ) -> TicketReceipt: ...


class FakeTicketWriter(TicketWriteConnector):
    def __init__(self, state: TicketState, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.state, self.clock = state, clock

    async def issue(self, command: TicketCommand, ttl_seconds: int) -> ActionCredential:
        command = TicketCommand.model_validate(command)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("动作凭证必须在 1–300 秒内到期")
        now = self.clock().astimezone(UTC)
        self.state.credentials = {
            key: value for key, value in self.state.credentials.items() if value.expires_at > now
        }
        credential = ActionCredential(
            token=SecretStr(secrets.token_urlsafe(32)),
            execution_id=command.execution_id,
            command_hash=ticket_command_hash(command),
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self.state.credentials[credential.token.get_secret_value()] = credential
        self.state.issue_count += 1
        return credential

    async def execute(self, command: TicketCommand, credential: ActionCredential) -> TicketReceipt:
        command, credential = (
            TicketCommand.model_validate(command),
            ActionCredential.model_validate(credential),
        )
        now = self.clock().astimezone(UTC)
        if not (
            self.state.credentials.get(credential.token.get_secret_value()) == credential
            and credential.command_hash == ticket_command_hash(command)
            and credential.execution_id == command.execution_id
            and credential.issued_at <= now < credential.expires_at
        ):
            raise PermissionError("凭证过期、伪造或超出批准动作范围")
        previous = self.state.receipts.get(command.execution_id)
        if previous:
            if previous.command_hash != credential.command_hash:
                raise PermissionError("相同执行 ID 不得用于其他动作")
            return previous
        ticket = self.state.tickets[command.ticket_id]
        if (
            ticket.service_name != command.service_name
            or ticket.updated_at != command.expected_updated_at
            or ticket.status != "open"
        ):
            raise ValueError("工单已变化或目标不属于批准服务")
        key = (command.service_name, command.grant.subject_id, command.grant.resource)
        if command.name == "grant_ticket_permission":
            if command.grant.expires_at <= now or key in self.state.grants:
                raise ValueError("权限申请已到期或已有权限，须重新分析")
            self.state.grants[key] = command.grant
        else:
            if self.state.grants.get(key) != command.grant or not command.resolution:
                raise ValueError("关闭工单前必须读回批准权限并提供回填")
            self.state.tickets[ticket.id] = ticket.model_copy(
                update={"status": "closed", "resolution": command.resolution, "updated_at": now}
            )
        receipt = TicketReceipt(
            execution_id=command.execution_id,
            command_hash=credential.command_hash,
            ticket_id=ticket.id,
            status="closed" if command.name == "close_ticket" else "open",
            completed_at=now,
        )
        self.state.receipts[command.execution_id] = receipt
        self.state.execution_count += 1
        return receipt


class HTTPTicketWriter(TicketWriteConnector):
    """待公司动作端确认的协议，只允许 HTTP mock，禁止真实生产写入。"""

    def __init__(
        self,
        config: OpsPlatformConfig,
        reader: ReaderCredentials,
        executor: ExecutorCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        validate_credential_separation(reader, executor)
        if (
            reader.connector != "ops_platform"
            or transport is None
            or not isinstance(transport, httpx.MockTransport)
        ):
            raise ValueError("工单真实动作端尚未验收，只允许独立身份的 HTTP mock")
        self.http = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": "Bearer " + executor.token.get_secret_value()},
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def issue(self, command: TicketCommand, ttl_seconds: int) -> ActionCredential:
        response = await self.http.post(
            "api/ticket-actions/authorize",
            json={"command": command.model_dump(mode="json"), "ttl_seconds": ttl_seconds},
        )
        if not response.is_success:
            raise PermissionError("动作端拒绝签发")
        credential = ActionCredential.model_validate_json(response.content)
        if (
            credential.command_hash != ticket_command_hash(command)
            or credential.execution_id != command.execution_id
            or (credential.expires_at - credential.issued_at).total_seconds() > ttl_seconds
        ):
            raise PermissionError("动作端凭证范围不匹配")
        return credential

    async def execute(self, command: TicketCommand, credential: ActionCredential) -> TicketReceipt:
        if (
            credential.command_hash != ticket_command_hash(command)
            or not credential.issued_at <= utc_now() < credential.expires_at
        ):
            raise PermissionError("动作凭证无效")
        response = await self.http.post(
            "api/ticket-actions/execute",
            json=command.model_dump(mode="json"),
            headers={"Authorization": "Bearer " + credential.token.get_secret_value()},
        )
        if not response.is_success:
            raise RuntimeError("动作端执行失败")
        return TicketReceipt.model_validate_json(response.content)

    async def aclose(self) -> None:
        await self.http.aclose()
