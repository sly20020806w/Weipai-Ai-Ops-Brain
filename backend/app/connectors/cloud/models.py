"""按服务读取的最小云资源状态；不保存原始云响应或指标序列。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Product = Literal["ecs", "rds", "redis", "mq", "slb", "vpc", "dns", "cdn"]
APIProduct = Literal["ecs", "rds", "redis", "mq", "slb", "vpc", "dns", "cdn", "cms"]
ServiceName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
ResourceID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")]
RegionID = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Text = Annotated[str, Field(min_length=1, max_length=512)]


class Snapshot(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class CloudQuery(Snapshot):
    service_name: ServiceName
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def window(self) -> "CloudQuery":
        if (self.start is None) != (self.end is None):
            raise ValueError("start 与 end 必须同时提供或同时省略")
        if self.start is not None and self.end is not None:
            if not timedelta(0) < self.end - self.start <= timedelta(days=1):
                raise ValueError("UTC 时间窗必须 start < end 且不超过 24 小时")
        return self

    def resolve(self, now: datetime) -> "CloudWindow":
        return CloudWindow(
            service_name=self.service_name,
            start=self.start if self.start is not None else now - timedelta(minutes=15),
            end=self.end if self.end is not None else now,
        )


class CloudWindow(CloudQuery):
    start: AwareDatetime
    end: AwareDatetime

    def contains(self, value: datetime) -> bool:
        return self.start <= value < self.end


class ResourceDetails(Snapshot):
    engine: Text | None = None
    engine_version: Text | None = None
    instance_type: Text | None = None
    vpc_id: ResourceID | None = None
    zone_id: Text | None = None
    cname: Text | None = None
    in_black_hole: bool | None = None
    in_clean: bool | None = None


class RDSConnections(Snapshot):
    availability: Literal["available", "no_data", "unsupported_engine"]
    max_connections: int | None = Field(default=None, ge=0)
    sampled_at: AwareDatetime | None = None
    active_connections: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    total_connections: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    metric_key: Literal["MySQL_Sessions"] | None = "MySQL_Sessions"

    @field_validator("sampled_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def values(self) -> "RDSConnections":
        if (self.availability == "unsupported_engine") != (self.metric_key is None):
            raise ValueError("不支持的引擎不能声称查询了 MySQL 指标")
        sample = (self.sampled_at, self.active_connections, self.total_connections)
        if self.availability == "available":
            if any(value is None for value in sample):
                raise ValueError("可用的连接数必须含采样时间、活跃和总连接数")
            assert self.active_connections is not None and self.total_connections is not None
            if self.active_connections > self.total_connections:
                raise ValueError("活跃连接数不能大于总连接数")
        elif any(value is not None for value in sample):
            raise ValueError("缺失的连接数不能伪造为零或携带采样值")
        return self


class CloudResource(Snapshot):
    service_name: ServiceName
    product: Product
    resource_id: ResourceID
    region_id: RegionID
    status: Text | None
    source_ref: Text
    details: ResourceDetails = Field(default_factory=ResourceDetails)
    rds_connections: RDSConnections | None = None

    @model_validator(mode="after")
    def connections(self) -> "CloudResource":
        if (self.product == "rds") != (self.rds_connections is not None):
            raise ValueError("只有 RDS 资源且所有 RDS 资源须声明连接数观测状态")
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.product, self.region_id, self.resource_id


class CloudEvent(Snapshot):
    id: Text
    service_name: ServiceName
    product: Product
    resource_id: ResourceID
    region_id: RegionID
    timestamp: AwareDatetime
    name: Text
    level: Literal["CRITICAL", "WARN", "INFO"]
    status: Text
    source_ref: Text

    @field_validator("timestamp")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class CloudTopic(Snapshot):
    region_id: RegionID
    instance_id: ResourceID
    name: ResourceID


class CloudResources(CloudWindow):
    source: Literal["alibaba_cloud"] = "alibaba_cloud"
    scope: Literal["configured_service_bindings"] = "configured_service_bindings"
    collected_at: AwareDatetime
    resources: tuple[CloudResource, ...]
    events: tuple[CloudEvent, ...]

    @field_validator("collected_at")
    @classmethod
    def collected_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def scoped(self) -> "CloudResources":
        identities = {resource.identity for resource in self.resources}
        if len(identities) != len(self.resources):
            raise ValueError("云资源不能重复")
        if any(resource.service_name != self.service_name for resource in self.resources):
            raise ValueError("云资源必须属于查询服务")
        for resource in self.resources:
            sample = resource.rds_connections
            if sample is not None and sample.sampled_at is not None:
                if not self.contains(sample.sampled_at):
                    raise ValueError("RDS 采样必须在查询时间窗内")
        if len({event.id for event in self.events}) != len(self.events):
            raise ValueError("云事件不能重复")
        for event in self.events:
            if (
                event.service_name != self.service_name
                or not self.contains(event.timestamp)
                or (event.product, event.region_id, event.resource_id) not in identities
            ):
                raise ValueError("云事件超出服务、资源或时间范围")
        return self
