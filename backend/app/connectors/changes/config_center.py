"""版本化配置 GET/JSON 适配协议；读取前白名单裁剪，不保存完整配置。"""

from urllib.parse import quote

import httpx2 as httpx

from app.connectors.changes.base import ChangesResponseError, ConfigCenterConnector
from app.connectors.changes.config import ConfigCenterConfig
from app.connectors.changes.http import ReaderHTTP, instant, obj, payload, rows, string, unique
from app.connectors.changes.models import (
    ChangeRecord,
    ConfigComparison,
    ConfigVersion,
    DeploymentQuery,
    VersionQuery,
    config_diff,
)
from app.connectors.models import ReaderCredentials


class HTTPConfigCenterConnector(ConfigCenterConnector):
    def __init__(
        self,
        config: ConfigCenterConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        self._config = ConfigCenterConfig.model_validate(config)
        self._http = ReaderHTTP(self._config, credentials, "config_center", transport)

    async def aclose(self) -> None:
        await self._http.client.aclose()

    async def list_changes(self, query: DeploymentQuery) -> tuple[ChangeRecord, ...]:
        query = DeploymentQuery.model_validate(query)
        target = self._http.target(query.service_name)
        records: list[ChangeRecord] = []
        seen: list[str] = []
        for page in range(1, self._config.max_pages + 1):
            data = obj(
                payload(
                    await self._http.read(
                        f"services/{quote(target, safe='')}/versions",
                        {
                            "start": query.start.isoformat(),
                            "end": query.end.isoformat(),
                            "page": str(page),
                            "page_size": str(self._config.page_size),
                        },
                    )
                )
            )
            if data.get("service_name") != query.service_name:
                raise ChangesResponseError("配置历史返回其他服务")
            for item in rows(data.get("versions")):
                identity = string(item.get("id"))
                seen.append(identity)
                timestamp = instant(item.get("published_at"))
                if query.contains(timestamp):
                    records.append(
                        ChangeRecord(
                            id=identity,
                            service_name=query.service_name,
                            source="config_center",
                            kind="Config",
                            timestamp=timestamp,
                            revision=string(item.get("version")),
                            source_ref=f"config_center:{target}:{identity}",
                        )
                    )
            unique(seen)
            next_page = data.get("next_page")
            if "next_page" not in data or (
                next_page is not None and (type(next_page) is not int or next_page != page + 1)
            ):
                raise ChangesResponseError("配置历史缺少完整分页标记或页码不连续")
            if next_page is None:
                return tuple(records)
        raise ChangesResponseError("配置历史超出分页上限，结果不完整")

    async def compare_versions(self, query: VersionQuery) -> ConfigComparison:
        query = VersionQuery.model_validate(query)
        target = self._http.target(query.service_name)
        versions: list[ConfigVersion] = []
        for version in (query.from_version, query.to_version):
            path = f"services/{quote(target, safe='')}/versions/{quote(version, safe='')}"
            data = obj(payload(await self._http.read(path)))
            if data.get("service_name") != query.service_name or data.get("version") != version:
                raise ChangesResponseError("配置中心响应与服务或版本不符")
            values = obj(data.get("values"))
            versions.append(
                ConfigVersion(
                    service_name=query.service_name,
                    version=version,
                    values={
                        key: string(values[key])
                        for key in self._config.allowed_keys
                        if key in values
                    },
                )
            )
        return config_diff(versions[0], versions[1])
