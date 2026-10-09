"""阿里云固定 GET 资源详情、RDS 最近采样和完整分页的 CMS 系统事件。"""

import json
import math
from datetime import UTC, datetime, timedelta

import httpx2 as httpx
from pydantic import JsonValue

from app.connectors.cloud.base import CloudConnector, CloudError, CloudNotFound, CloudResponseError
from app.connectors.cloud.config import CloudConfig, ResourceBinding
from app.connectors.cloud.http import READ_APIS, CloudHTTP, integer, obj, rows, text
from app.connectors.cloud.models import (
    CloudEvent,
    CloudQuery,
    CloudResource,
    CloudResources,
    CloudTopic,
    CloudWindow,
    RDSConnections,
    ResourceDetails,
)
from app.connectors.models import ReaderCredentials


def optional_text(value: JsonValue) -> str | None:
    return None if value is None or value == "" else text(value)


def instant(value: JsonValue) -> datetime:
    result = datetime.fromisoformat(text(value))
    if result.utcoffset() is None:
        raise CloudResponseError("阿里云采样时间必须包含时区")
    return result.astimezone(UTC)


class HTTPCloudConnector(CloudConnector):
    def __init__(
        self,
        config: CloudConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        assert self.reader_credentials is not None
        # model_dump + 重新校验，隔离宿主字典和 model_copy 绕过校验的配置。
        self._config = CloudConfig.model_validate(config.model_dump())
        self._http = CloudHTTP(self._config, self.reader_credentials, transport)

    async def aclose(self) -> None:
        await self._http.client.aclose()

    async def list_topics(self, service_name: str) -> tuple[CloudTopic, ...]:
        query = CloudQuery(service_name=service_name)
        bindings = self._config.services.get(query.service_name)
        if bindings is None:
            raise CloudNotFound("服务没有配置阿里云资源绑定")
        topics: list[CloudTopic] = []
        try:
            for binding in bindings:
                if binding.product != "mq":
                    continue
                data = await self._call(
                    "OnsTopicList",
                    {
                        "RegionId": binding.region_id,
                        "InstanceId": binding.resource_id,
                    },
                )
                for record in rows(obj(data["Data"])["PublishInfoDo"]):
                    if record["InstanceId"] != binding.resource_id:
                        raise CloudResponseError("MQ Topic 返回未绑定实例")
                    topics.append(
                        CloudTopic(
                            region_id=binding.region_id,
                            instance_id=binding.resource_id,
                            name=text(record["Topic"]),
                        )
                    )
            if len({(t.region_id, t.instance_id, t.name) for t in topics}) != len(topics):
                raise CloudResponseError("MQ Topic 返回重复记录")
            return tuple(topics)
        except (ValueError, KeyError, TypeError):
            raise CloudResponseError("MQ Topic 响应协议不符") from None

    async def _call(self, action: str, params: dict[str, str]) -> dict[str, JsonValue]:
        api = next(api for api in READ_APIS if api.action == action)
        return await self._http.read(api, params)

    async def _connections(
        self,
        binding: ResourceBinding,
        engine: str,
        maximum: int | None,
        window: CloudWindow,
    ) -> RDSConnections:
        if engine not in {"MySQL", "MariaDB"}:
            return RDSConnections(
                availability="unsupported_engine", max_connections=maximum, metric_key=None
            )
        start = window.start.replace(second=0, microsecond=0)
        end = window.end.replace(second=0, microsecond=0)
        if end < window.end:
            end += timedelta(minutes=1)
        data = await self._call(
            "DescribeDBInstancePerformance",
            {
                "RegionId": binding.region_id,
                "DBInstanceId": binding.resource_id,
                "Key": "MySQL_Sessions",
                "StartTime": start.strftime("%Y-%m-%dT%H:%MZ"),
                "EndTime": end.strftime("%Y-%m-%dT%H:%MZ"),
            },
        )
        if data["DBInstanceId"] != binding.resource_id or data["Engine"] != engine:
            raise CloudResponseError("RDS 性能响应实例或引擎不符")
        keys = rows(obj(data["PerformanceKeys"])["PerformanceKey"])
        if not keys:
            return RDSConnections(availability="no_data", max_connections=maximum)
        if len(keys) != 1 or keys[0]["Key"] != "MySQL_Sessions":
            raise CloudResponseError("RDS 性能响应指标不符")
        fields = text(keys[0]["ValueFormat"]).split("&")
        if len(fields) != 2 or set(fields) != {"active_session", "total_session"}:
            raise CloudResponseError("RDS 连接数 ValueFormat 无法识别")
        samples = rows(obj(keys[0]["Values"])["PerformanceValue"])
        if len(samples) > 20000:
            raise CloudResponseError("RDS 采样超出读取上限")
        seen: set[datetime] = set()
        result: RDSConnections | None = None
        for sample in samples:
            timestamp = instant(sample["Date"])
            if timestamp in seen:
                raise CloudResponseError("RDS 采样时间重复")
            seen.add(timestamp)
            values = text(sample["Value"]).split("&")
            if len(values) != 2:
                raise CloudResponseError("RDS 连接数采样格式不符")
            counts = dict(zip(fields, map(float, values), strict=True))
            observation = RDSConnections(
                availability="available",
                max_connections=maximum,
                sampled_at=timestamp,
                active_connections=counts["active_session"],
                total_connections=counts["total_session"],
            )
            if window.contains(timestamp) and (
                result is None or result.sampled_at is not None and timestamp > result.sampled_at
            ):
                result = observation
        return result or RDSConnections(availability="no_data", max_connections=maximum)

    async def _resource(
        self,
        service: str,
        binding: ResourceBinding,
        window: CloudWindow,
    ) -> CloudResource:
        product = binding.product
        params = {"RegionId": binding.region_id}
        connection: RDSConnections | None = None
        if product == "ecs":
            data = await self._call(
                "DescribeInstances",
                {
                    **params,
                    "InstanceIds": json.dumps([binding.resource_id], separators=(",", ":")),
                    "PageNumber": "1",
                    "PageSize": "1",
                },
            )
            records = rows(obj(data["Instances"])["Instance"])
            if integer(data["TotalCount"]) != len(records) or len(records) != 1:
                raise CloudResponseError("ECS 单资源查询缺失或不完整")
            data = records[0]
            id_field, status_field = "InstanceId", "Status"
        elif product in {"rds", "redis"}:
            action = (
                "DescribeDBInstanceAttribute" if product == "rds" else "DescribeInstanceAttribute"
            )
            id_field = "DBInstanceId" if product == "rds" else "InstanceId"
            root = "Items" if product == "rds" else "Instances"
            response = await self._call(action, {**params, id_field: binding.resource_id})
            records = rows(obj(response[root])["DBInstanceAttribute"])
            if len(records) != 1:
                raise CloudResponseError("数据库单资源查询缺失或不完整")
            data = records[0]
            status_field = "DBInstanceStatus" if product == "rds" else "InstanceStatus"
        elif product == "mq":
            response = await self._call(
                "OnsInstanceBaseInfo", {**params, "InstanceId": binding.resource_id}
            )
            data = obj(response["InstanceBaseInfo"])
            id_field, status_field = "InstanceId", "InstanceStatus"
        elif product == "slb":
            data = await self._call(
                "DescribeLoadBalancerAttribute", {**params, "LoadBalancerId": binding.resource_id}
            )
            id_field, status_field = "LoadBalancerId", "LoadBalancerStatus"
        elif product == "vpc":
            data = await self._call(
                "DescribeVpcAttribute", {**params, "VpcId": binding.resource_id}
            )
            id_field, status_field = "VpcId", "Status"
        elif product == "dns":
            data = await self._call("DescribeDomainInfo", {"DomainName": binding.resource_id})
            id_field, status_field = "DomainName", ""
        else:
            response = await self._call(
                "DescribeCdnDomainDetail", {"DomainName": binding.resource_id}
            )
            data = obj(response["GetDomainDetailModel"])
            id_field, status_field = "DomainName", "DomainStatus"
        if data[id_field] != binding.resource_id:
            raise CloudResponseError("阿里云返回未绑定资源")
        if "RegionId" in data and data["RegionId"] != binding.region_id:
            raise CloudResponseError("阿里云返回资源地域不符")
        status = (
            str(integer(data[status_field]))
            if product == "mq"
            else text(data[status_field])
            if status_field
            else None
        )
        engine = text(data["Engine"]) if product == "rds" else None
        if product == "rds":
            assert engine is not None
            maximum = integer(data["MaxConnections"]) if "MaxConnections" in data else None
            connection = await self._connections(binding, engine, maximum, window)
        # 不透传 Tags、Description、灾备配置、云事件 Content 等任意字段。
        details = ResourceDetails.model_validate(
            {
                "engine": engine,
                "engine_version": optional_text(data.get("EngineVersion"))
                if product in {"rds", "redis"}
                else None,
                "instance_type": optional_text(data.get("InstanceType"))
                if product == "ecs"
                else None,
                "vpc_id": optional_text(data.get("VpcId"))
                if product in {"rds", "redis", "slb"}
                else None,
                "zone_id": optional_text(data.get("ZoneId"))
                if product in {"ecs", "rds", "redis"}
                else None,
                "cname": optional_text(data.get("Cname")) if product == "cdn" else None,
                "in_black_hole": data.get("InBlackHole") if product == "dns" else None,
                "in_clean": data.get("InClean") if product == "dns" else None,
            }
        )
        return CloudResource(
            service_name=service,
            product=product,
            resource_id=binding.resource_id,
            region_id=binding.region_id,
            status=status,
            details=details,
            rds_connections=connection,
            source_ref=f"alibaba:{product}:{binding.region_id}:{binding.resource_id}",
        )

    async def _events(
        self,
        bindings: tuple[ResourceBinding, ...],
        window: CloudWindow,
    ) -> tuple[CloudEvent, ...]:
        if not bindings:
            return ()
        targets = {(b.region_id, b.event_resource_id or b.resource_id): b for b in bindings}
        events: list[CloudEvent] = []
        seen: set[str] = set()
        # CMS 返回时间窗内的账号事件，本地按准确的地域 + 资源绑定裁剪。
        for page in range(1, self._config.max_pages + 1):
            data = await self._call(
                "DescribeSystemEventAttribute",
                {
                    "StartTime": str(math.floor(window.start.timestamp() * 1000)),
                    "EndTime": str(math.ceil(window.end.timestamp() * 1000)),
                    "PageNumber": str(page),
                    "PageSize": str(self._config.page_size),
                },
            )
            records = rows(obj(data["SystemEvents"])["SystemEvent"])
            if len(records) > self._config.page_size:
                raise CloudResponseError("云事件分页大小不符")
            for record in records:
                event_id = text(record["Id"])
                if event_id in seen:
                    raise CloudResponseError("云事件分页重复，结果不完整")
                seen.add(event_id)
                timestamp = datetime.fromtimestamp(integer(record["Time"]) / 1000, UTC)
                binding = targets.get((text(record["RegionId"]), text(record["ResourceId"])))
                if binding is None or not window.contains(timestamp):
                    continue
                events.append(
                    CloudEvent.model_validate(
                        {
                            "id": event_id,
                            "service_name": window.service_name,
                            "product": binding.product,
                            "resource_id": binding.resource_id,
                            "region_id": binding.region_id,
                            "timestamp": timestamp,
                            "name": record["Name"],
                            "level": record["Level"],
                            "status": record["Status"],
                            "source_ref": f"alibaba:cms:{event_id}",
                        }
                    )
                )
            if len(records) < self._config.page_size:
                return tuple(
                    sorted(events, key=lambda event: (event.timestamp, event.id), reverse=True)
                )
        raise CloudResponseError("云事件超出分页上限，结果不完整")

    async def get_cloud_resources(self, query: CloudQuery) -> CloudResources:
        query = CloudQuery.model_validate(query.model_dump())
        bindings = self._config.services.get(query.service_name)
        if bindings is None:
            raise CloudNotFound("服务没有配置阿里云资源绑定")
        if self._http.client.is_closed:
            raise CloudError("阿里云 Connector 已关闭")
        now = datetime.now(UTC)
        window = query.resolve(now)
        try:
            resources = tuple(
                [await self._resource(query.service_name, b, window) for b in bindings]
            )
            events = await self._events(bindings, window)
            return CloudResources(
                **window.model_dump(),
                collected_at=datetime.now(UTC),
                resources=tuple(sorted(resources, key=lambda resource: resource.identity)),
                events=events,
            )
        except (ValueError, KeyError, TypeError, OverflowError, OSError):
            raise CloudResponseError("阿里云资源或事件响应协议不符") from None
