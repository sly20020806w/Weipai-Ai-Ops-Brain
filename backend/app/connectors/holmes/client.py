"""原生 /api/chat 快照适配；外部 I/O 只存在于 Connector 中。"""

import json
from abc import abstractmethod

import httpx2 as httpx

from app.connectors.base import ReadOnlyConnector
from app.connectors.holmes.models import HolmesConfig, HolmesRequest, HolmesResponse
from app.connectors.models import ReaderCredentials


class HolmesError(RuntimeError):
    pass


class HolmesConnector(ReadOnlyConnector):
    @abstractmethod
    async def analyze(self, request: HolmesRequest) -> HolmesResponse: ...


class HTTPHolmesConnector(HolmesConnector):
    def __init__(
        self,
        config: HolmesConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        self._config = HolmesConfig.model_validate(config)
        # 原生 Holmes 服务可能自主运行工具。其工具隔离尚未接入本平台的
        # Dispatcher，当前只交付协议适配与 Fake；禁止实际发送 HTTP 请求。
        if transport is None:
            raise HolmesError("Holmes 原生工具隔离尚未验收，只允许 Fake 或 HTTP mock")
        assert self.reader_credentials is not None
        if self.reader_credentials.connector != "holmes":
            raise ValueError("Holmes 只接受独立的 holmes Reader 凭证")
        token = self.reader_credentials.token.get_secret_value()
        if any(char.isspace() or ord(char) < 32 for char in token):
            raise ValueError("Holmes Reader 凭证不能包含空白或控制字符")
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TypeError("测试只接受 MockTransport")
        self._http = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._config.timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def analyze(self, request: HolmesRequest) -> HolmesResponse:
        request = HolmesRequest.model_validate(request)
        if self._http.is_closed:
            raise HolmesError("Holmes Connector 已关闭")
        payload = {
            "ask": request.model_dump_json(),
            "model": self._config.model,
            "stream": False,
            "enable_tool_approval": True,
            "additional_system_prompt": "仅分析给定证据快照，禁止调用任何工具。仅返回 JSON 意见。",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "expert_opinion", "schema": request.output_schema},
            },
        }
        try:
            response = await self._http.post("api/chat", json=payload)
            if not response.is_success:
                raise HolmesError("Holmes 返回非成功状态")
            body = json.loads(response.content)
            if (
                not isinstance(body, dict)
                or body.get("tool_calls")
                or body.get("approval_required")
            ):
                raise HolmesError("Holmes 响应试图运行工具")
            return HolmesResponse.model_validate({"analysis": body.get("analysis")})
        except HolmesError:
            raise
        except Exception:
            raise HolmesError("Holmes 快照分析失败") from None

    async def aclose(self) -> None:
        await self._http.aclose()
