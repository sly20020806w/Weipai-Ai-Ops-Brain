"""本包的固定 GET 传输与严格解析；不跟随响应中的 URL，不自建重试。"""

from datetime import datetime

import httpx2 as httpx
from pydantic import JsonValue, TypeAdapter

from app.connectors.changes.base import (
    ChangesError,
    ChangesHTTPError,
    ChangesNotFound,
    ChangesResponseError,
    ChangesTimeout,
)
from app.connectors.changes.config import EndpointConfig
from app.connectors.models import ReaderCredentials


def obj(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ChangesResponseError("变更链路响应必须是对象")
    return value


def rows(value: JsonValue) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list):
        raise ChangesResponseError("变更链路响应必须是列表")
    return [obj(item) for item in value]


def string(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ChangesResponseError("变更链路字段必须是非空字符串")
    return value


def integer(value: JsonValue) -> int:
    if type(value) is not int:
        raise ChangesResponseError("变更链路字段必须是整数")
    return value


def instant(value: JsonValue) -> datetime:
    try:
        result = datetime.fromisoformat(string(value))
        if result.utcoffset() is None:
            raise ValueError
        return result
    except ValueError:
        raise ChangesResponseError("变更链路时间必须包含时区") from None


def unique(values: list[str]) -> None:
    if len(set(values)) != len(values):
        raise ChangesResponseError("变更链路响应含重复记录")


class ReaderHTTP:
    def __init__(
        self,
        config: EndpointConfig,
        credentials: ReaderCredentials,
        name: str,
        transport: httpx.MockTransport | None,
        *,
        username: str | None = None,
    ) -> None:
        if credentials.connector != name:
            raise ValueError(f"只接受 {name} 的 Reader 凭证")
        token = credentials.token.get_secret_value()
        if any(char.isspace() or ord(char) < 32 for char in token):
            raise ValueError("变更链路 Reader token 不能包含空白或控制字符")
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TypeError("测试 transport 只接受 MockTransport")
        self.config = EndpointConfig.model_validate(
            {key: getattr(config, key) for key in EndpointConfig.model_fields}
        )
        headers = {"Accept": "application/json"}
        auth: httpx.BasicAuth | None = None
        if name == "ci" and username is not None:
            auth = httpx.BasicAuth(username, token)
        elif name in {"git", "ci"} and getattr(config, "provider", None) in {"gitlab", "gitlab_ci"}:
            headers["PRIVATE-TOKEN"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
        if name == "git" and getattr(config, "provider", None) == "github":
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        self.client = httpx.AsyncClient(
            base_url=self.config.base_url,
            headers=headers,
            auth=auth,
            timeout=self.config.timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def target(self, service: str) -> str:
        target = self.config.services.get(service)
        if target is None:
            raise ChangesNotFound("服务没有配置变更来源绑定")
        return target

    async def read(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        if self.client.is_closed:
            raise ChangesError("变更链路 Connector 已关闭")
        try:
            response = await self.client.get(path, params=params)
        except httpx.TimeoutException:
            raise ChangesTimeout("变更链路读取超时") from None
        except httpx.RequestError:
            raise ChangesError("变更链路连接失败") from None
        if response.status_code == 404:
            raise ChangesNotFound("变更链路记录不存在")
        if response.status_code != 200:
            raise ChangesHTTPError(response.status_code)
        return response


def payload(response: httpx.Response) -> JsonValue:
    try:
        return TypeAdapter(JsonValue).validate_json(response.content)
    except ValueError:
        raise ChangesResponseError("变更链路响应不是有效 JSON") from None
