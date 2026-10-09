"""Executor 的纯数据契约；不包含身份密钥或任务状态写入。"""

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from app.connectors.kubernetes.models import Namespace, ResourceName, ServiceName
from app.tasks.workflow_models import ApprovalPrompt, TaskSnapshot
from app.tools.models import ToolModel

ActionName = Literal[
    "restart_service", "scale_service", "rollback_prod", "deploy_service", "pause_release"
]
Text = Annotated[str, StringConstraints(min_length=1, max_length=2048, pattern=r"^\S+$")]


class ExecutionBinding(ToolModel):
    service_name: ServiceName
    cluster_name: Text
    namespace: Namespace
    deployment_name: ResourceName
    container_name: ResourceName
    images: dict[str, Text]
    min_replicas: int = Field(default=1, ge=1, le=10000)
    max_replicas: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def bounds(self) -> "ExecutionBinding":
        if self.max_replicas < self.min_replicas or not self.images:
            raise ValueError("动作绑定必须有镜像白名单和有效副本范围")
        return self


class ExecutionConfig(ToolModel):
    enabled: bool = False
    credential_ttl_seconds: int = Field(default=60, ge=1, le=300)
    bindings: tuple[ExecutionBinding, ...] = ()

    @model_validator(mode="after")
    def unique_services(self) -> "ExecutionConfig":
        if len({b.service_name for b in self.bindings}) != len(self.bindings):
            raise ValueError("每个服务只能绑定一个明确执行目标")
        return self


class ExecutionTarget(ToolModel):
    service_name: ServiceName
    cluster_name: Text
    namespace: Namespace
    deployment_name: ResourceName
    container_name: ResourceName
    uid: Text
    resource_version: Text
    image: Text
    replicas: int = Field(ge=1, le=10000)
    paused: bool = Field(default=False, exclude_if=lambda value: value is False)
    traffic_percent: int = Field(default=100, ge=1, le=100, exclude_if=lambda value: value == 100)


class TargetQuery(ToolModel):
    service_name: ServiceName
    cluster_name: Text
    namespace: Namespace
    deployment_name: ResourceName
    container_name: ResourceName


class ExecutionCommand(ToolModel):
    execution_id: UUID
    task_id: UUID
    plan_evidence_id: UUID
    plan_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    action_id: Text
    name: ActionName
    target: ExecutionTarget
    expected_image: Text
    expected_replicas: int = Field(ge=1, le=10000)
    expected_paused: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    expected_traffic_percent: int | None = Field(
        default=None, ge=1, le=100, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def minimal_change(self) -> "ExecutionCommand":
        if self.name in {"deploy_service", "pause_release"}:
            if self.expected_replicas != self.target.replicas:
                raise ValueError("发布和暂停不能修改副本数")
            if self.name == "pause_release" and (
                self.expected_image != self.target.image
                or self.expected_paused is not True
                or self.expected_traffic_percent != self.target.traffic_percent
            ):
                raise ValueError("暂停只能停止当前发布，不能改变镜像或流量")
            if self.name == "deploy_service" and (
                self.expected_paused is not False or self.expected_traffic_percent is None
            ):
                raise ValueError("发布必须有精确灰度流量目标")
        elif self.name != "rollback_prod" and (
            self.expected_paused is not None or self.expected_traffic_percent is not None
        ):
            raise ValueError("其他动作不能修改发布状态")
        if self.name in {"restart_service", "scale_service"}:
            if self.expected_image != self.target.image:
                raise ValueError("重启与扩缩容不能修改镜像")
        if self.name in {"restart_service", "rollback_prod"}:
            if self.expected_replicas != self.target.replicas:
                raise ValueError("重启与回滚不能修改副本数")
        if self.name == "rollback_prod" and self.expected_image == self.target.image:
            raise ValueError("回滚必须修改镜像")
        return self


def command_hash(command: ExecutionCommand) -> str:
    return hashlib.sha256(
        json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ExecutionRequest:
    task: TaskSnapshot
    plan_evidence_id: str
    approval: ApprovalPrompt | None = None


@dataclass(frozen=True)
class ExecutionResult:
    task: TaskSnapshot
    evidence_ids: list[str]
