"""可注入、快照隔离的 Fake；样例不携带任何真实身份。"""

from datetime import UTC, datetime

from app.connectors.cloud.base import CloudConnector, CloudError, CloudNotFound
from app.connectors.cloud.models import (
    CloudEvent,
    CloudQuery,
    CloudResource,
    CloudResources,
    CloudTopic,
    Product,
    RDSConnections,
    ResourceDetails,
)

SAMPLE_START = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
SAMPLE_END = datetime(2026, 10, 1, 1, 10, tzinfo=UTC)
SAMPLE_TIME = datetime(2026, 10, 1, 1, 8, tzinfo=UTC)
SAMPLE_IDS: dict[Product, str] = {
    "ecs": "i-payment",
    "rds": "rm-payment",
    "redis": "r-payment",
    "mq": "MQ_INST_payment",
    "slb": "lb-payment",
    "vpc": "vpc-payment",
    "dns": "payment.example.com",
    "cdn": "static.payment.example.com",
}


def sample_resources() -> tuple[CloudResource, ...]:
    statuses: dict[Product, str | None] = {
        "ecs": "Running",
        "rds": "Running",
        "redis": "Normal",
        "mq": "5",
        "slb": "active",
        "vpc": "Available",
        "dns": None,
        "cdn": "online",
    }
    return tuple(
        CloudResource(
            service_name="payment-service",
            product=product,
            resource_id=resource_id,
            region_id="cn-hangzhou",
            status=statuses[product],
            source_ref=f"alibaba:{product}:cn-hangzhou:{resource_id}",
            details=ResourceDetails(engine="MySQL", engine_version="8.0", vpc_id="vpc-payment")
            if product == "rds"
            else ResourceDetails(),
            rds_connections=RDSConnections(
                availability="available",
                max_connections=600,
                sampled_at=SAMPLE_TIME,
                active_connections=480.0,
                total_connections=520.0,
            )
            if product == "rds"
            else None,
        )
        for product, resource_id in SAMPLE_IDS.items()
    )


def sample_events() -> tuple[CloudEvent, ...]:
    return (
        CloudEvent(
            id="event-rds-connections",
            service_name="payment-service",
            product="rds",
            resource_id="rm-payment",
            region_id="cn-hangzhou",
            timestamp=SAMPLE_TIME,
            name="RDSConnectionUsageHigh",
            level="WARN",
            status="alert",
            source_ref="alibaba:cms:event-rds-connections",
        ),
    )


class FakeCloudConnector(CloudConnector):
    def __init__(
        self,
        *,
        resources: tuple[CloudResource, ...] | None = None,
        events: tuple[CloudEvent, ...] | None = None,
        topics: tuple[CloudTopic, ...] | None = None,
    ) -> None:
        super().__init__()
        self._resources = tuple(
            CloudResource.model_validate(r.model_dump())
            for r in (sample_resources() if resources is None else resources)
        )
        self._events = tuple(
            CloudEvent.model_validate(e.model_dump())
            for e in (sample_events() if events is None else events)
        )
        if len({(r.service_name, *r.identity) for r in self._resources}) != len(self._resources):
            raise ValueError("Fake 云资源不能重复")
        if len({e.id for e in self._events}) != len(self._events):
            raise ValueError("Fake 云事件不能重复")
        for event in self._events:
            if not any(
                (r.service_name, *r.identity)
                == (event.service_name, event.product, event.region_id, event.resource_id)
                for r in self._resources
            ):
                raise ValueError("Fake 云事件必须关联已有资源")
        self._closed = False
        self._topics = (
            topics
            if topics is not None
            else (
                CloudTopic(
                    region_id="cn-hangzhou", instance_id="MQ_INST_payment", name="payment-events"
                ),
            )
        )

    async def aclose(self) -> None:
        self._closed = True

    async def list_topics(self, service_name: str) -> tuple[CloudTopic, ...]:
        if self._closed:
            raise CloudError("阿里云 Connector 已关闭")
        query = CloudQuery(service_name=service_name)
        targets = {
            (r.region_id, r.resource_id)
            for r in self._resources
            if r.product == "mq" and r.service_name == query.service_name
        }
        return tuple(
            CloudTopic.model_validate(t).model_copy(deep=True)
            for t in self._topics
            if (t.region_id, t.instance_id) in targets
        )

    async def get_cloud_resources(self, query: CloudQuery) -> CloudResources:
        if self._closed:
            raise CloudError("阿里云 Connector 已关闭")
        query = CloudQuery.model_validate(query.model_dump())
        window = query.resolve(SAMPLE_END)
        resources: list[CloudResource] = []
        for resource in self._resources:
            if resource.service_name != query.service_name:
                continue
            copy = CloudResource.model_validate(resource.model_dump())
            sample = copy.rds_connections
            if (
                sample is not None
                and sample.sampled_at is not None
                and not window.contains(sample.sampled_at)
            ):
                copy = CloudResource.model_validate(
                    {
                        **copy.model_dump(),
                        "rds_connections": RDSConnections(
                            availability="no_data", max_connections=sample.max_connections
                        ),
                    }
                )
            resources.append(copy)
        if not resources:
            raise CloudNotFound("服务没有 Fake 阿里云资源")
        return CloudResources(
            **window.model_dump(),
            collected_at=SAMPLE_END,
            resources=tuple(sorted(resources, key=lambda resource: resource.identity)),
            events=tuple(
                sorted(
                    (
                        CloudEvent.model_validate(e.model_dump())
                        for e in self._events
                        if e.service_name == query.service_name and window.contains(e.timestamp)
                    ),
                    key=lambda event: (event.timestamp, event.id),
                    reverse=True,
                )
            ),
        )
