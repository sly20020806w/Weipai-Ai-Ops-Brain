"""SLS GetLogs 搜索接口，完整分页后返回服务与时间窗内的最小日志快照。"""

import math
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx2 as httpx
from pydantic import TypeAdapter, ValidationError

from app.connectors.models import ReaderCredentials
from app.connectors.observability.base import ObservabilityResponseError, SLSConnector
from app.connectors.observability.config import SLSConfig
from app.connectors.observability.http import ReaderHTTP, reader_key, sls_headers
from app.connectors.observability.models import LogRecord, Window


class HTTPSLSConnector(SLSConnector):
    def __init__(
        self,
        config: SLSConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        assert self.reader_credentials is not None
        if self.reader_credentials.connector != "sls":
            raise ValueError("SLS 只接受 sls Reader 凭证")
        self._config = SLSConfig.model_validate(config)
        if not (urlsplit(self._config.base_url).hostname or "").startswith(
            self._config.project + "."
        ):
            raise ValueError("SLS 端点必须属于配置的 project")
        self._key = reader_key(self.reader_credentials)
        self._http = ReaderHTTP(self._config, transport)

    async def aclose(self) -> None:
        await self._http.client.aclose()

    async def query_logs(self, query: Window) -> tuple[LogRecord, ...]:
        query = Window.model_validate(query)
        path = f"/logstores/{self._config.logstore}"
        params = {
            "type": "log",
            "from": str(math.floor(query.start.timestamp())),
            "to": str(math.ceil(query.end.timestamp())),
            "topic": "",
            "query": f'{self._config.service_field}: "{query.service_name}"',
            "line": str(self._config.page_size),
            "offset": "0",
            "reverse": "false",
        }
        result: list[LogRecord] = []
        for index in range(self._config.max_pages):
            params["offset"] = str(index * self._config.page_size)
            response = await self._http.read(path, params, sls_headers(self._key, path, params))
            try:
                records = TypeAdapter(list[dict[str, str]]).validate_json(
                    response.content, strict=True
                )
                if response.headers.get("x-log-progress") != "Complete":
                    raise ValueError("日志查询尚未完成")
                count = int(response.headers["x-log-count"])
                if count != len(records) or count > self._config.page_size:
                    raise ValueError("日志分页数量不一致")
                for record in records:
                    service = record[self._config.service_field]
                    if service != query.service_name:
                        raise ValueError("日志服务筛选不一致")
                    timestamp = datetime.fromtimestamp(int(record["__time__"]), UTC)
                    item = LogRecord(
                        service_name=service,
                        timestamp=timestamp,
                        level=record[self._config.level_field],
                        message=record[self._config.message_field],
                        source_ref=f"sls:{self._config.project}/{self._config.logstore}",
                    )
                    if query.contains(timestamp):
                        result.append(item)
            except (ValidationError, ValueError, KeyError, OverflowError, OSError):
                raise ObservabilityResponseError("SLS 响应不符合完整的日志协议") from None
            if count < self._config.page_size:
                return tuple(sorted(result, key=lambda item: item.timestamp))
        raise ObservabilityResponseError("SLS 超出分页上限，结果不完整")
