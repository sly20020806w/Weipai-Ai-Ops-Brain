"""独立写入通道：动作级授权服务契约，Fake 实施精确参数限制和幂等执行。

原生 RBAC 无法限制同一资源的具体参数，不能将普通 SA token 包装为动作级权限。
HTTP 端须由已有运维平台的受信动作端实施同等限制；当前只开放 MockTransport。
"""

import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID

import httpx2 as httpx
from pydantic import AwareDatetime, Field, SecretStr, field_validator, model_validator

from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.models import (
    ExecutorCredentials,
    ReaderCredentials,
    validate_credential_separation,
)
from app.db.base import utc_now
from app.executor.models import (
    ExecutionBinding,
    ExecutionCommand,
    ExecutionTarget,
    TargetQuery,
    command_hash,
)
from app.tools.models import ToolModel


class ActionCredential(ToolModel):
    token: SecretStr = Field(repr=False)
    execution_id: UUID
    command_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def short_lived(self) -> "ActionCredential":
        duration = (self.expires_at - self.issued_at).total_seconds()
        if not 0 < duration <= 300 or not self.token.get_secret_value().strip():
            raise ValueError("动作凭证必须非空且至多有效 300 秒")
        return self


class ExecutionReceipt(ToolModel):
    execution_id: UUID
    command_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: AwareDatetime
    target: ExecutionTarget

    @field_validator("completed_at")
    @classmethod
    def utc_completion(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


def validate_capability(
    command: ExecutionCommand, credential: ActionCredential, now: datetime
) -> None:
    credential = ActionCredential.model_validate(credential)
    if now.tzinfo is None or not (
        credential.execution_id == command.execution_id
        and credential.command_hash == command_hash(command)
        and credential.issued_at <= now < credential.expires_at
    ):
        raise PermissionError("动作凭证过期或超出精确动作范围")


class KubernetesWriteConnector(ABC):
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    @abstractmethod
    async def inspect(self, binding: TargetQuery) -> ExecutionTarget: ...

    @abstractmethod
    async def issue(self, command: ExecutionCommand, ttl_seconds: int) -> ActionCredential: ...

    @abstractmethod
    async def execute(
        self, command: ExecutionCommand, credential: ActionCredential
    ) -> ExecutionReceipt: ...

    @abstractmethod
    async def aclose(self) -> None: ...


def fake_binding() -> ExecutionBinding:
    return ExecutionBinding(
        service_name="payment-service",
        cluster_name="ack-fake",
        namespace="payment",
        deployment_name="payment-service",
        container_name="payment",
        images={v: f"registry.example.invalid/payment:{v}" for v in ("v2.3.6", "v2.3.7")},
    )


class FakeKubernetesWriteConnector(KubernetesWriteConnector):
    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        binding = fake_binding()
        self.targets = {
            binding.service_name: ExecutionTarget(
                service_name=binding.service_name,
                cluster_name=binding.cluster_name,
                namespace=binding.namespace,
                deployment_name=binding.deployment_name,
                container_name=binding.container_name,
                uid="deployment-payment",
                resource_version="7",
                image=binding.images["v2.3.7"],
                replicas=3,
            )
        }
        self.clock = clock
        self.credentials: dict[str, ActionCredential] = {}
        self.receipts: dict[UUID, ExecutionReceipt] = {}
        self.execution_count = 0
        self.issue_count = 0
        self.closed = False

    def check_open(self) -> None:
        if self.closed:
            raise RuntimeError("动作 Connector 已关闭")

    async def inspect(self, binding: TargetQuery) -> ExecutionTarget:
        self.check_open()
        target = self.targets[binding.service_name]
        if any(
            getattr(target, f) != getattr(binding, f)
            for f in ("cluster_name", "namespace", "deployment_name", "container_name")
        ):
            raise ValueError("执行目标与服务绑定不一致")
        return target.model_copy(deep=True)

    async def issue(self, command: ExecutionCommand, ttl_seconds: int) -> ActionCredential:
        self.check_open()
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("动作凭证有效期必须为 1–300 秒")
        command = ExecutionCommand.model_validate(command)
        now = self.clock().astimezone(UTC)
        self.credentials = {
            token: issued for token, issued in self.credentials.items() if issued.expires_at > now
        }
        credential = ActionCredential(
            token=SecretStr(secrets.token_urlsafe(32)),
            execution_id=command.execution_id,
            command_hash=command_hash(command),
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self.credentials[credential.token.get_secret_value()] = credential.model_copy(deep=True)
        self.issue_count += 1
        return credential

    async def execute(
        self, command: ExecutionCommand, credential: ActionCredential
    ) -> ExecutionReceipt:
        self.check_open()
        command = ExecutionCommand.model_validate(command)
        credential = ActionCredential.model_validate(credential)
        validate_capability(command, credential, self.clock())
        if self.credentials.get(credential.token.get_secret_value()) != credential:
            raise PermissionError("动作凭证不是由受信签发端签发")
        previous = self.receipts.get(command.execution_id)
        if previous:
            if previous.command_hash != command_hash(command):
                raise PermissionError("相同幂等键不能用于其他动作")
            return previous.model_copy(deep=True)
        target = self.targets.get(command.target.service_name)
        if target != command.target:
            raise ValueError("资源已变更或目标不符，拒绝过期动作")
        target = target.model_copy(
            update={
                "image": command.expected_image,
                "replicas": command.expected_replicas,
                "resource_version": str(int(target.resource_version) + 1),
                "paused": target.paused
                if command.expected_paused is None
                else command.expected_paused,
                "traffic_percent": target.traffic_percent
                if command.expected_traffic_percent is None
                else command.expected_traffic_percent,
            }
        )
        receipt = ExecutionReceipt(
            execution_id=command.execution_id,
            command_hash=command_hash(command),
            completed_at=self.clock().astimezone(UTC),
            target=target,
        )
        self.targets[target.service_name] = target
        self.receipts[command.execution_id] = receipt
        self.execution_count += 1
        return receipt.model_copy(deep=True)

    async def aclose(self) -> None:
        self.closed = True
        self.credentials.clear()


class HTTPKubernetesWriteConnector(KubernetesWriteConnector):
    """已有动作端的显式 GET/POST 协议；签发身份与 Reader 分离。

    服务端须实施精确命令哈希/目标/有效期及持久化 execution_id 去重。
    不使用该 token 访问原生 Kubernetes API，也不自行构造真实生产权限。
    """

    def __init__(
        self,
        config: KubernetesConfig,
        reader: ReaderCredentials,
        executor: ExecutorCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        validate_credential_separation(reader, executor)
        if reader.connector != "kubernetes":
            raise ValueError("动作通道需要 Kubernetes 身份")
        if transport is None or not isinstance(transport, httpx.MockTransport):
            raise ValueError("真实动作端协议与服务器参数限制尚未验收；只允许 HTTP mock")
        config = KubernetesConfig.model_validate(config)
        self.reader, self.executor = reader, executor
        self.http = httpx.AsyncClient(
            base_url=config.base_url,
            transport=transport,
            timeout=config.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )

    async def request(self, method: str, path: str, token: SecretStr, payload: object) -> object:
        try:
            response = await self.http.request(
                method,
                path,
                json=payload,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            )
            if response.status_code != 200:
                raise RuntimeError("动作端响应失败")
            return response.json()
        except Exception:
            raise RuntimeError("动作端请求失败，需按幂等键核对结果") from None

    async def inspect(self, binding: TargetQuery) -> ExecutionTarget:
        return ExecutionTarget.model_validate_json(
            json_encode(
                await self.request(
                    "POST", "execution/inspect", self.reader.token, binding.model_dump(mode="json")
                )
            )
        )

    async def issue(self, command: ExecutionCommand, ttl_seconds: int) -> ActionCredential:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("动作凭证有效期必须为 1–300 秒")
        credential = ActionCredential.model_validate_json(
            json_encode(
                await self.request(
                    "POST",
                    "execution/credentials",
                    self.executor.token,
                    {"command": command.model_dump(mode="json"), "ttl_seconds": ttl_seconds},
                )
            )
        )
        validate_capability(command, credential, utc_now())
        if (credential.expires_at - credential.issued_at).total_seconds() > ttl_seconds:
            raise PermissionError("动作端签发的凭证超过请求有效期")
        if credential.token in {self.reader.token, self.executor.token}:
            raise PermissionError("动作凭证不可复用 Reader 或签发身份")
        return credential

    async def execute(
        self, command: ExecutionCommand, credential: ActionCredential
    ) -> ExecutionReceipt:
        validate_capability(command, credential, utc_now())
        receipt = ExecutionReceipt.model_validate_json(
            json_encode(
                await self.request(
                    "POST", "execution/actions", credential.token, command.model_dump(mode="json")
                )
            )
        )
        if (
            receipt.command_hash != command_hash(command)
            or receipt.execution_id != command.execution_id
        ):
            raise ValueError("动作回执与请求不符")
        return receipt

    async def aclose(self) -> None:
        await self.http.aclose()


def json_encode(value: object) -> str:
    import json

    return json.dumps(value)
