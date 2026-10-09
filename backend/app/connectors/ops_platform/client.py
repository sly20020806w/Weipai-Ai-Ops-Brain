"""只读接口与 HTTP 实现；分页无重试，生命周期由宿主/后续 Temporal 管理。"""

from abc import abstractmethod

import httpx2 as httpx
from pydantic import TypeAdapter, ValidationError

from app.connectors.base import ReadOnlyConnector
from app.connectors.models import ReaderCredentials
from app.connectors.ops_platform.config import OpsPlatformConfig
from app.connectors.ops_platform.models import (
    Application,
    Identifier,
    Owner,
    Page,
    ServiceTreeNode,
    SourceRecord,
    Ticket,
)

_identifier = TypeAdapter(Identifier)


class OpsPlatformError(RuntimeError):
    """异常不输出 Reader 凭证、请求 URL 或源系统错误正文。"""


class OpsPlatformNotFound(OpsPlatformError):
    pass


class OpsPlatformResponseError(OpsPlatformError):
    pass


class OpsPlatformTimeout(OpsPlatformError):
    pass


class OpsPlatformTransportError(OpsPlatformError):
    pass


class OpsPlatformHTTPError(OpsPlatformError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"运维平台返回 HTTP {status_code}")


def checked_identifier(value: str) -> str:
    return _identifier.validate_python(value, strict=True)


class OpsPlatformConnector(ReadOnlyConnector):
    @abstractmethod
    async def list_service_tree(self) -> tuple[ServiceTreeNode, ...]: ...

    @abstractmethod
    async def list_applications(
        self, *, business_id: str | None = None
    ) -> tuple[Application, ...]: ...

    @abstractmethod
    async def get_application(self, service_name: str) -> Application: ...

    @abstractmethod
    async def list_owners(self, service_name: str) -> tuple[Owner, ...]: ...

    @abstractmethod
    async def list_tickets(
        self, *, service_name: str | None = None, status: str | None = None
    ) -> tuple[Ticket, ...]: ...

    @abstractmethod
    async def get_ticket(self, ticket_id: str) -> Ticket: ...


class HTTPOpsPlatformConnector(OpsPlatformConnector):
    def __init__(
        self,
        config: OpsPlatformConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        assert self.reader_credentials is not None
        if self.reader_credentials.connector != "ops_platform":
            raise ValueError("运维平台只接受 ops_platform 的 Reader 凭证")
        token = self.reader_credentials.token.get_secret_value()
        if any(char.isspace() or ord(char) < 32 for char in token):
            raise ValueError("运维平台 Reader token 不能包含空白或控制字符")
        self._config = OpsPlatformConfig.model_validate(config)
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TypeError("测试 transport 只接受 MockTransport")
        self._http = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self._config.timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _read[Record: SourceRecord](
        self, path: str, page_type: type[Page[Record]], params: dict[str, str] | None = None
    ) -> tuple[Record, ...]:
        if self._http.is_closed:
            raise OpsPlatformError("运维平台 Connector 已关闭")
        query = dict(params or {})
        query["limit"] = str(self._config.page_size)
        items: list[Record] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        for _ in range(self._config.max_pages):
            try:
                response = await self._http.get(path, params=query)
            except httpx.TimeoutException:
                raise OpsPlatformTimeout("运维平台读取超时") from None
            except httpx.RequestError:
                raise OpsPlatformTransportError("运维平台连接失败") from None
            if response.status_code == 404:
                raise OpsPlatformNotFound("运维平台记录或读取路径不存在")
            if not response.is_success:
                raise OpsPlatformHTTPError(response.status_code)
            try:
                page = page_type.model_validate_json(response.content)
            except (ValidationError, ValueError):
                raise OpsPlatformResponseError("运维平台响应不符合只读适配协议") from None
            for item in page.items:
                record_id: str = item.id
                if record_id in seen_ids:
                    raise OpsPlatformResponseError("运维平台分页出现重复记录")
                seen_ids.add(record_id)
                items.append(item)
            cursor = page.next_cursor
            if cursor is None:
                return tuple(items)
            if cursor in seen_cursors:
                raise OpsPlatformResponseError("运维平台分页游标重复")
            seen_cursors.add(cursor)
            query["cursor"] = cursor
        raise OpsPlatformResponseError("运维平台读取超出分页上限，结果不完整")

    async def list_service_tree(self) -> tuple[ServiceTreeNode, ...]:
        return await self._read(self._config.service_tree_path, Page[ServiceTreeNode])

    async def list_applications(self, *, business_id: str | None = None) -> tuple[Application, ...]:
        params = {"business_id": checked_identifier(business_id)} if business_id is not None else {}
        items = await self._read(self._config.applications_path, Page[Application], params)
        if business_id is not None and any(item.business_id != business_id for item in items):
            raise OpsPlatformResponseError("运维平台应用响应与业务筛选条件不符")
        return items

    async def get_application(self, service_name: str) -> Application:
        items = await self._read(
            self._config.applications_path,
            Page[Application],
            {"service_name": checked_identifier(service_name)},
        )
        if not items:
            raise OpsPlatformNotFound("运维平台应用不存在")
        if len(items) != 1 or items[0].service_name != service_name:
            raise OpsPlatformResponseError("运维平台应用响应与目标服务不符或不唯一")
        return items[0]

    async def list_owners(self, service_name: str) -> tuple[Owner, ...]:
        return await self._read(
            self._config.owners_path,
            Page[Owner],
            {"service_name": checked_identifier(service_name)},
        )

    async def list_tickets(
        self, *, service_name: str | None = None, status: str | None = None
    ) -> tuple[Ticket, ...]:
        params = {
            name: checked_identifier(value)
            for name, value in (("service_name", service_name), ("status", status))
            if value is not None
        }
        items = await self._read(self._config.tickets_path, Page[Ticket], params)
        if any(
            (service_name is not None and item.service_name != service_name)
            or (status is not None and item.status != status)
            for item in items
        ):
            raise OpsPlatformResponseError("运维平台工单响应与筛选条件不符")
        return items

    async def get_ticket(self, ticket_id: str) -> Ticket:
        items = await self._read(
            self._config.tickets_path, Page[Ticket], {"ticket_id": checked_identifier(ticket_id)}
        )
        if not items:
            raise OpsPlatformNotFound("运维平台工单不存在")
        if len(items) != 1 or items[0].id != ticket_id:
            raise OpsPlatformResponseError("运维平台工单响应与目标 ID 不符或不唯一")
        return items[0]
