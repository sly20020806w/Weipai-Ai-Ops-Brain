"""Kubernetes API 的最小证据快照；丢弃 env、Secret 等无关原始字段。"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Namespace = Annotated[
    str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
]
ServiceName = Annotated[
    str, Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
]
ResourceName = Annotated[
    str, Field(min_length=1, max_length=253, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
]
Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]
NonNegative = Annotated[int, Field(ge=0)]


class SnapshotModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=True,
        populate_by_name=True,
        serialize_by_alias=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ObjectMeta(SnapshotModel):
    uid: Identifier
    name: ResourceName
    namespace: Namespace
    labels: dict[str, str] = Field(default_factory=dict)
    generation: NonNegative | None = None
    resource_version: Identifier | None = Field(default=None, alias="resourceVersion")


class Condition(SnapshotModel):
    type: Identifier
    status: Literal["True", "False", "Unknown"]
    reason: str | None = None


class DeploymentSpec(SnapshotModel):
    replicas: NonNegative = 1


class DeploymentStatus(SnapshotModel):
    observed_generation: NonNegative | None = Field(default=None, alias="observedGeneration")
    replicas: NonNegative = 0
    ready_replicas: NonNegative = Field(default=0, alias="readyReplicas")
    available_replicas: NonNegative = Field(default=0, alias="availableReplicas")
    updated_replicas: NonNegative = Field(default=0, alias="updatedReplicas")
    conditions: tuple[Condition, ...] = ()


class Deployment(SnapshotModel):
    api_version: Literal["apps/v1"] = Field(alias="apiVersion")
    kind: Literal["Deployment"]
    metadata: ObjectMeta
    spec: DeploymentSpec
    status: DeploymentStatus = Field(default_factory=DeploymentStatus)


class Container(SnapshotModel):
    name: ResourceName
    image: str = Field(min_length=1, max_length=2048)


class PodSpec(SnapshotModel):
    node_name: ResourceName | None = Field(default=None, alias="nodeName")
    containers: tuple[Container, ...] = Field(min_length=1)


class ContainerStatus(SnapshotModel):
    name: ResourceName
    ready: bool
    restart_count: NonNegative = Field(alias="restartCount")


class PodStatus(SnapshotModel):
    # Pending Pod 刚创建时可能还没有 status.phase。
    phase: Literal["Pending", "Running", "Succeeded", "Failed", "Unknown"] | None = None
    conditions: tuple[Condition, ...] = ()
    container_statuses: tuple[ContainerStatus, ...] = Field(default=(), alias="containerStatuses")


class Pod(SnapshotModel):
    api_version: Literal["v1"] = Field(alias="apiVersion")
    kind: Literal["Pod"]
    metadata: ObjectMeta
    spec: PodSpec
    status: PodStatus = Field(default_factory=PodStatus)


class ObjectReference(SnapshotModel):
    uid: Identifier | None = None
    kind: Identifier
    name: ResourceName
    namespace: Namespace | None = None

    @field_validator("namespace", mode="before")
    @classmethod
    def normalize_cluster_scope(cls, value: object) -> object:
        return None if value == "" else value


class Event(SnapshotModel):
    api_version: Literal["v1"] = Field(alias="apiVersion")
    kind: Literal["Event"]
    metadata: ObjectMeta
    involved_object: ObjectReference = Field(alias="involvedObject")
    type: Literal["Normal", "Warning"]
    reason: str = Field(min_length=1, max_length=1024)
    message: str = Field(default="", max_length=100_000)
    count: NonNegative | None = None
    first_timestamp: AwareDatetime | None = Field(default=None, alias="firstTimestamp")
    last_timestamp: AwareDatetime | None = Field(default=None, alias="lastTimestamp")
    event_time: AwareDatetime | None = Field(default=None, alias="eventTime")

    @field_validator("first_timestamp", "last_timestamp", "event_time")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def check_reference(self) -> "Event":
        if (
            self.involved_object.namespace is not None
            and self.involved_object.namespace != self.metadata.namespace
        ):
            raise ValueError("Event 与关联对象必须属于同一命名空间")
        if (
            self.first_timestamp
            and self.last_timestamp
            and self.last_timestamp < self.first_timestamp
        ):
            raise ValueError("Event 最后时间不能早于首次时间")
        return self


class ListMeta(SnapshotModel):
    continue_token: str = Field(default="", alias="continue", max_length=16384)
    resource_version: Identifier | None = Field(default=None, alias="resourceVersion")


class NamespaceMeta(SnapshotModel):
    uid: Identifier
    name: Namespace


class NamespaceRecord(SnapshotModel):
    api_version: Literal["v1"] = Field(alias="apiVersion")
    kind: Literal["Namespace"]
    metadata: NamespaceMeta


class ResourceList[Record: SnapshotModel](SnapshotModel):
    api_version: str = Field(alias="apiVersion")
    kind: str
    metadata: ListMeta
    items: tuple[Record, ...]


class KubernetesSnapshot(SnapshotModel):
    deployments: tuple[Deployment, ...]
    pods: tuple[Pod, ...]
    events: tuple[Event, ...]

    @model_validator(mode="after")
    def unique_objects(self) -> "KubernetesSnapshot":
        objects: tuple[Deployment | Pod | Event, ...] = (
            *self.deployments,
            *self.pods,
            *self.events,
        )
        if len({item.metadata.uid for item in objects}) != len(objects):
            raise ValueError("Fake 集群的对象 UID 不能重复")
        keys = {(item.kind, item.metadata.namespace, item.metadata.name) for item in objects}
        if len(keys) != len(objects):
            raise ValueError("Fake 集群同命名空间同类型对象名称不能重复")
        return self
