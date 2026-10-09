"""仅供本包固定 GET 接口使用，签名不向 Tool 暴露；不自建重试。"""

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import quote
from uuid import uuid4

import httpx2 as httpx
from pydantic import ValidationError

from app.connectors.models import ReaderCredentials
from app.connectors.observability.base import (
    ObservabilityError,
    ObservabilityHTTPError,
    ObservabilityResponseError,
    ObservabilityTimeout,
)
from app.connectors.observability.config import AlibabaReaderKey, EndpointConfig


class ReaderHTTP:
    def __init__(self, config: EndpointConfig, transport: httpx.MockTransport | None) -> None:
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TypeError("测试 transport 只接受 MockTransport")
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def read(
        self, path: str, params: dict[str, str], headers: dict[str, str]
    ) -> httpx.Response:
        if self.client.is_closed:
            raise ObservabilityError("可观测性 Connector 已关闭")
        try:
            response = await self.client.get(path, params=params, headers=headers)
        except httpx.TimeoutException:
            raise ObservabilityTimeout("可观测性读取超时") from None
        except httpx.RequestError:
            raise ObservabilityError("可观测性连接失败") from None
        if response.status_code != 200:
            raise ObservabilityHTTPError(response.status_code)
        return response


def reader_key(credentials: ReaderCredentials) -> AlibabaReaderKey:
    try:
        return AlibabaReaderKey.model_validate_json(credentials.token.get_secret_value())
    except (ValueError, ValidationError):
        raise ValueError("SLS/ARMS Reader token 必须是包含 AccessKey 的凭证 JSON") from None


def sls_headers(key: AlibabaReaderKey, path: str, params: dict[str, str]) -> dict[str, str]:
    headers = {
        "x-log-apiversion": "0.6.0",
        "x-log-signaturemethod": "hmac-sha1",
        "x-log-bodyrawsize": "0",
        "Date": format_datetime(datetime.now(UTC), usegmt=True),
    }
    if key.security_token is not None:
        headers["x-acs-security-token"] = key.security_token.get_secret_value()
    canonical_headers = "".join(
        f"{name}:{value}\n" for name, value in sorted(headers.items()) if name.startswith("x-")
    )
    resource = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    message = "GET\n\n\n" + headers["Date"] + "\n" + canonical_headers + resource
    signature = base64.b64encode(
        hmac.digest(key.access_key_secret.get_secret_value().encode(), message.encode(), "sha1")
    ).decode()
    headers["Authorization"] = f"LOG {key.access_key_id.get_secret_value()}:{signature}"
    return headers


def arms_headers(
    key: AlibabaReaderKey, host: str, action: str, params: dict[str, str]
) -> dict[str, str]:
    headers = {
        "host": host,
        "x-acs-action": action,
        "x-acs-version": "2019-08-08",
        "x-acs-date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x-acs-signature-nonce": uuid4().hex,
        "x-acs-content-sha256": hashlib.sha256(b"").hexdigest(),
    }
    if key.security_token is not None:
        headers["x-acs-security-token"] = key.security_token.get_secret_value()
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{v.strip()}\n" for k, v in sorted(headers.items()))
    canonical_query = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(params.items())
    )
    canonical = "\n".join(
        [
            "GET",
            "/",
            canonical_query,
            canonical_headers,
            signed,
            headers["x-acs-content-sha256"],
        ]
    )
    message = "ACS3-HMAC-SHA256\n" + hashlib.sha256(canonical.encode()).hexdigest()
    signature = hmac.new(
        key.access_key_secret.get_secret_value().encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        f"ACS3-HMAC-SHA256 Credential={key.access_key_id.get_secret_value()},"
        f"SignedHeaders={signed},Signature={signature}"
    )
    return headers


def json_response(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        raise ObservabilityResponseError("可观测性响应不是 JSON") from None


def object_response(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ObservabilityResponseError("可观测性响应必须是 JSON 对象")
    return value
