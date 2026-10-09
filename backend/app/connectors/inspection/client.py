"""公司只读事实适配协议；真实字段/覆盖范围需按公司协议核对。"""

from abc import abstractmethod

import httpx2 as httpx
from pydantic import Field, field_validator

from app.connectors.base import ReadOnlyConnector
from app.connectors.inspection.models import InspectionFacts, InspectionQuery
from app.connectors.models import ReaderCredentials
from app.connectors.ops_platform.config import OpsPlatformConfig
from app.tools.models import ToolModel


class InspectionEndpoint(ToolModel):
    base_url: str
    path: str
    timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return OpsPlatformConfig.validate_base_url(value)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return OpsPlatformConfig.validate_path(value)

    def checked(self) -> "InspectionEndpoint":
        value = OpsPlatformConfig(
            base_url=self.base_url,
            service_tree_path=self.path,
            applications_path=self.path,
            owners_path=self.path,
            tickets_path=self.path,
            timeout_seconds=self.timeout_seconds,
        )
        return InspectionEndpoint(
            base_url=value.base_url, path=self.path, timeout_seconds=value.timeout_seconds
        )


class InspectionConnector(ReadOnlyConnector):
    @abstractmethod
    async def query(self, query: InspectionQuery) -> InspectionFacts: ...


class HTTPInspectionConnector(InspectionConnector):
    def __init__(
        self,
        endpoint: InspectionEndpoint,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        if credentials.connector != "inspection":
            raise ValueError("巡检 Reader 身份不匹配")
        self.endpoint = InspectionEndpoint.model_validate(endpoint).checked()
        self.http = httpx.AsyncClient(
            base_url=self.endpoint.base_url,
            timeout=self.endpoint.timeout_seconds,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": "Bearer " + credentials.token.get_secret_value()},
        )

    async def query(self, query: InspectionQuery) -> InspectionFacts:
        query = InspectionQuery.model_validate(query)
        try:
            response = await self.http.get(self.endpoint.path, params=query.model_dump())
            if response.status_code != 200:
                raise ValueError("查询失败")
            result = InspectionFacts.model_validate_json(response.content)
            if result.service_name != query.service_name:
                raise ValueError("服务范围错误")
            return result
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("巡检事实查询失败；未确认健康") from None

    async def aclose(self) -> None:
        await self.http.aclose()
