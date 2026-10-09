"""固定只读 API 清单与 ACS3 签名，禁止通用 API 调用和重定向。"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx2 as httpx
from pydantic import JsonValue, TypeAdapter

from app.connectors.cloud.base import CloudError, CloudHTTPError, CloudResponseError, CloudTimeout
from app.connectors.cloud.config import CloudConfig
from app.connectors.cloud.models import APIProduct
from app.connectors.models import ReaderCredentials
from app.connectors.observability.config import AlibabaReaderKey


@dataclass(frozen=True)
class ReadAPI:
    product: APIProduct
    action: str
    version: str


READ_APIS = (
    ReadAPI("ecs", "DescribeInstances", "2014-05-26"),
    ReadAPI("rds", "DescribeDBInstanceAttribute", "2014-08-15"),
    ReadAPI("rds", "DescribeDBInstancePerformance", "2014-08-15"),
    ReadAPI("redis", "DescribeInstanceAttribute", "2015-01-01"),
    ReadAPI("mq", "OnsInstanceBaseInfo", "2019-02-14"),
    ReadAPI("mq", "OnsTopicList", "2019-02-14"),
    ReadAPI("slb", "DescribeLoadBalancerAttribute", "2014-05-15"),
    ReadAPI("vpc", "DescribeVpcAttribute", "2016-04-28"),
    ReadAPI("dns", "DescribeDomainInfo", "2015-01-09"),
    ReadAPI("cdn", "DescribeCdnDomainDetail", "2018-05-10"),
    ReadAPI("cms", "DescribeSystemEventAttribute", "2019-01-01"),
)


def signed_headers(
    key: AlibabaReaderKey,
    host: str,
    api: ReadAPI,
    params: dict[str, str],
    *,
    timestamp: str,
    nonce: str,
) -> dict[str, str]:
    headers = {
        "host": host,
        "x-acs-action": api.action,
        "x-acs-version": api.version,
        "x-acs-date": timestamp,
        "x-acs-signature-nonce": nonce,
        "x-acs-content-sha256": hashlib.sha256(b"").hexdigest(),
    }
    if key.security_token is not None:
        headers["x-acs-security-token"] = key.security_token.get_secret_value()
    names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{v.strip()}\n" for k, v in sorted(headers.items()))
    canonical_query = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(params.items())
    )
    canonical = "\n".join(
        ["GET", "/", canonical_query, canonical_headers, names, headers["x-acs-content-sha256"]]
    )
    message = "ACS3-HMAC-SHA256\n" + hashlib.sha256(canonical.encode()).hexdigest()
    signature = hmac.new(
        key.access_key_secret.get_secret_value().encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        f"ACS3-HMAC-SHA256 Credential={key.access_key_id.get_secret_value()},"
        f"SignedHeaders={names},Signature={signature}"
    )
    return headers


def obj(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise CloudResponseError("阿里云响应字段必须是对象")
    return value


def rows(value: JsonValue) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list):
        raise CloudResponseError("阿里云响应字段必须是数组")
    return [obj(row) for row in value]


def text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CloudResponseError("阿里云响应字段必须是非空短字符串")
    return value


def integer(value: JsonValue) -> int:
    if type(value) is not int or value < 0:
        raise CloudResponseError("阿里云响应字段必须是非负整数")
    return value


class CloudHTTP:
    def __init__(
        self,
        config: CloudConfig,
        credentials: ReaderCredentials,
        transport: httpx.MockTransport | None,
    ) -> None:
        if credentials.connector != "cloud":
            raise ValueError("阿里云只接受 cloud Reader 凭证")
        try:
            self._key = AlibabaReaderKey.model_validate_json(credentials.token.get_secret_value())
        except ValueError:
            raise ValueError("cloud Reader token 必须是包含 AccessKey 的凭证 JSON") from None
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TypeError("测试 transport 只接受 MockTransport")
        self._config = config
        self.client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def read(self, api: ReadAPI, params: dict[str, str]) -> dict[str, JsonValue]:
        if api not in READ_APIS:
            raise CloudError("阿里云动作不在只读清单中")
        if self.client.is_closed:
            raise CloudError("阿里云 Connector 已关闭")
        endpoint = self._config.endpoints[api.product]
        headers = signed_headers(
            self._key,
            urlsplit(endpoint).netloc,
            api,
            params,
            timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            nonce=uuid4().hex,
        )
        # 使用和签名完全相同的 RFC3986 编码（不能把空格编码为 +）。
        query = "&".join(
            f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(params.items())
        )
        try:
            response = await self.client.get(endpoint + "?" + query, headers=headers)
        except httpx.TimeoutException:
            raise CloudTimeout("阿里云只读请求超时") from None
        except httpx.RequestError:
            raise CloudError("阿里云只读连接失败") from None
        if response.status_code != 200:
            raise CloudHTTPError(response.status_code)
        try:
            data = obj(TypeAdapter(JsonValue).validate_json(response.content))
        except ValueError:
            raise CloudResponseError("阿里云响应不是有效 JSON") from None
        if api.product == "cms":
            success = data.get("Success")
            if data.get("Code") != "200" or not (success is True or success == "true"):
                raise CloudResponseError("阿里云云事件返回业务错误")
        elif "Code" in data:
            raise CloudResponseError("阿里云返回业务错误")
        return data
