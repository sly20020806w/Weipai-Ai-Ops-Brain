"""GitLab CI 原生分页及 Jenkins builds tree 分片读取。"""

from datetime import UTC, datetime
from urllib.parse import quote

import httpx2 as httpx

from app.connectors.changes.base import ChangesResponseError, CIConnector
from app.connectors.changes.config import CIConfig
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
from app.connectors.changes.models import BuildRecord, DeploymentQuery
from app.connectors.models import ReaderCredentials


class HTTPCIConnector(CIConnector):
    def __init__(
        self,
        config: CIConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        self._config = CIConfig.model_validate(config)
        if config.provider == "jenkins" and config.username is None:
            raise ValueError("Jenkins 需要配置 Reader username")
        self._http = ReaderHTTP(
            self._config,
            credentials,
            "ci",
            transport,
            username=config.username if config.provider == "jenkins" else None,
        )

    async def aclose(self) -> None:
        await self._http.client.aclose()

    async def list_builds(self, query: DeploymentQuery) -> tuple[BuildRecord, ...]:
        query = DeploymentQuery.model_validate(query)
        target = self._http.target(query.service_name)
        records: list[BuildRecord] = []
        for page in range(1, self._config.max_pages + 1):
            if self._config.provider == "gitlab_ci":
                path = f"projects/{quote(target, safe='')}/pipelines"
                response = await self._http.read(
                    path,
                    {
                        "per_page": str(self._config.page_size),
                        "page": str(page),
                        "order_by": "id",
                        "sort": "desc",
                    },
                )
                items = rows(payload(response))
                next_page = response.headers.get("X-Next-Page")
                if next_page is None:
                    raise ChangesResponseError("GitLab CI 缺少分页完整性标记")
                if next_page and next_page != str(page + 1):
                    raise ChangesResponseError("GitLab CI 分页页码不连续")
                more = bool(next_page)
                for item in items:
                    records.append(
                        BuildRecord(
                            id=str(integer(item.get("id"))),
                            service_name=query.service_name,
                            timestamp=instant(item.get("created_at")),
                            source="gitlab_ci",
                            revision=string(item.get("sha")),
                            status=string(item.get("status")),
                            source_ref=f"gitlab_ci:{target}:{integer(item.get('id'))}",
                        )
                    )
            else:
                path = (
                    "/".join(f"job/{quote(part, safe='')}" for part in target.split("/"))
                    + "/api/json"
                )
                offset = (page - 1) * self._config.page_size
                tree = (
                    "builds[number,timestamp,result,building,actions[lastBuiltRevision[SHA1]]]"
                    f"{{{offset},{offset + self._config.page_size}}}"
                )
                items = rows(
                    obj(payload(await self._http.read(path, {"tree": tree}))).get("builds")
                )
                more = len(items) == self._config.page_size
                for item in items:
                    revision: str | None = None
                    for action in rows(item.get("actions", [])):
                        if action.get("lastBuiltRevision") is not None:
                            revision = string(obj(action["lastBuiltRevision"]).get("SHA1"))
                    status = item.get("result")
                    if status is None and item.get("building") is not True:
                        raise ChangesResponseError("Jenkins 缺少构建结果")
                    timestamp = integer(item.get("timestamp"))
                    try:
                        created = datetime.fromtimestamp(timestamp / 1000, UTC)
                    except (ValueError, OverflowError, OSError):
                        raise ChangesResponseError("Jenkins 时间戳无效") from None
                    number = integer(item.get("number"))
                    records.append(
                        BuildRecord(
                            id=str(number),
                            service_name=query.service_name,
                            timestamp=created,
                            source="jenkins",
                            revision=revision,
                            status="running" if status is None else string(status),
                            source_ref=f"jenkins:{target}:{number}",
                        )
                    )
            unique([item.id for item in records])
            if not more:
                return tuple(
                    sorted(
                        (item for item in records if query.contains(item.timestamp)),
                        key=lambda item: (item.timestamp, item.id),
                        reverse=True,
                    )
                )
        raise ChangesResponseError("构建读取超出分页上限，结果不完整")
