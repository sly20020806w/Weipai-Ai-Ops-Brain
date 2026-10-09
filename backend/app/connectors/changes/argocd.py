"""读取 ArgoCD Application 的保留发布历史，不触发 refresh/sync。"""

from urllib.parse import quote

import httpx2 as httpx

from app.connectors.changes.base import ArgoCDConnector, ChangesResponseError
from app.connectors.changes.config import ArgoCDConfig
from app.connectors.changes.http import (
    ReaderHTTP,
    instant,
    integer,
    obj,
    payload,
    rows,
    string,
    unique,
)
from app.connectors.changes.models import DeploymentQuery, DeploymentRecord
from app.connectors.models import ReaderCredentials


class HTTPArgoCDConnector(ArgoCDConnector):
    def __init__(
        self,
        config: ArgoCDConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        self._config = ArgoCDConfig.model_validate(config)
        self._http = ReaderHTTP(self._config, credentials, "argocd", transport)

    async def aclose(self) -> None:
        await self._http.client.aclose()

    async def list_deployments(self, query: DeploymentQuery) -> tuple[DeploymentRecord, ...]:
        query = DeploymentQuery.model_validate(query)
        target = self._http.target(query.service_name)
        data = obj(payload(await self._http.read(f"api/v1/applications/{quote(target, safe='')}")))
        if string(obj(data.get("metadata")).get("name")) != target:
            raise ChangesResponseError("ArgoCD 响应与应用绑定不符")
        status = obj(data.get("status"))
        records: list[DeploymentRecord] = []
        for item in rows(status.get("history", [])):
            # 多源 Application 有多个 revision，不能错误映射成一个服务版本。
            if item.get("revisions") or item.get("sources"):
                raise ChangesResponseError("当前只读适配器仅支持单源 ArgoCD Application")
            record_id = str(integer(item.get("id")))
            records.append(
                DeploymentRecord(
                    id=record_id,
                    service_name=query.service_name,
                    application=target,
                    revision=string(item.get("revision")),
                    timestamp=instant(item.get("deployedAt")),
                    source_ref=f"argocd:{target}:{record_id}",
                )
            )
        unique([item.id for item in records])
        return tuple(
            sorted(
                (item for item in records if query.contains(item.timestamp)),
                key=lambda item: (item.timestamp, item.id),
                reverse=True,
            )
        )
