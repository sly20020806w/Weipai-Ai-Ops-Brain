"""只调用现有只读 Connector；按真实引用构建带来源的关系。"""

import hashlib
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import Settings
from app.connectors.changes.base import GitConnector
from app.connectors.changes.factory import create_git_connector
from app.connectors.cloud.base import CloudConnector
from app.connectors.cloud.factory import create_cloud_connector
from app.connectors.cloud.models import CloudQuery
from app.connectors.kubernetes.client import KubernetesConnector
from app.connectors.kubernetes.factory import create_kubernetes_connector
from app.connectors.observability.base import ARMSConnector
from app.connectors.observability.factory import create_arms_connector
from app.connectors.observability.fake import FakeARMSConnector, discovery_traces
from app.connectors.observability.models import Window, topology
from app.connectors.ops_platform.client import OpsPlatformConnector
from app.connectors.ops_platform.factory import create_ops_platform_connector
from app.graph.discovery.models import DiscoverySnapshot, NodeRef, Relation


def node(kind: str, identity: str, name: str | None = None) -> NodeRef:
    # 带范围的 ID 常比单个源 ID 长；摘要保证不会超出既有图字段。
    external_id = (
        identity
        if len(identity) <= 300
        else "sha256:" + hashlib.sha256(identity.encode()).hexdigest()
    )
    return NodeRef(kind=kind, external_id=external_id, name=(name or identity)[:300])


@dataclass
class DiscoverySources:
    ops: OpsPlatformConnector
    k8s: KubernetesConnector
    arms: ARMSConnector
    git: GitConnector
    cloud: CloudConnector
    git_services: frozenset[str]
    cloud_services: frozenset[str]

    async def collect(self, end: datetime, lookback_seconds: int) -> DiscoverySnapshot:
        nodes: dict[tuple[str, str], NodeRef] = {}
        relations: dict[tuple[tuple[str, str], tuple[str, str], str, str], Relation] = {}
        missing: list[str] = []

        def remember(value: NodeRef) -> NodeRef:
            old = nodes.get(value.key)
            if old is None or value.name != value.external_id:
                nodes[value.key] = value
            return nodes[value.key]

        def link(
            origin: NodeRef,
            target: NodeRef,
            relation: str,
            source: str,
            confidence: float = 1.0,
            observed_at: datetime = end,
        ) -> None:
            remember(origin)
            remember(target)
            key = origin.key, target.key, relation, source
            old = relations.get(key)
            if old is None or old.observed_at < observed_at:
                relations[key] = Relation(
                    origin=origin,
                    target=target,
                    relation=relation,
                    source=source,
                    confidence=confidence,
                    observed_at=observed_at,
                )

        tree = await self.ops.list_service_tree()
        businesses = {item.id: node("business", item.id, item.name) for item in tree}
        for item in tree:
            remember(businesses[item.id])
            if item.parent_id is not None:
                if item.parent_id not in businesses:
                    raise ValueError("服务树引用不存在的父节点")
                link(businesses[item.parent_id], businesses[item.id], "contains", "ops_platform")
        applications = await self.ops.list_applications()
        if len({app.service_name for app in applications}) != len(applications):
            raise ValueError("服务名不唯一，无法准确归属关系")
        namespaces = await self.k8s.list_namespaces()
        cluster = node("cluster", self.k8s.cluster_name)
        remember(cluster)
        for app in applications:
            service = remember(node("service", app.service_name, app.name))
            if app.business_id not in businesses:
                raise ValueError("应用的业务归属不存在")
            link(service, businesses[app.business_id], "belongs_to", "ops_platform")
            owners = await self.ops.list_owners(app.service_name)
            if {owner.id for owner in owners} != set(app.owner_ids):
                raise ValueError("应用的负责人引用不完整")
            for owner in owners:
                link(service, node("owner", owner.id, owner.name), "owned_by", "ops_platform")
            for namespace in namespaces:
                deployments = await self.k8s.list_deployments(
                    namespace, service_name=app.service_name
                )
                pods = await self.k8s.list_pods(namespace, service_name=app.service_name)
                if deployments or pods:
                    link(service, cluster, "runs_in", "kubernetes")
                for deployment in deployments:
                    ref = node(
                        "deployment",
                        f"{cluster.external_id}:{deployment.metadata.uid}",
                        deployment.metadata.name,
                    )
                    link(service, ref, "has_deployment", "kubernetes")
                    link(ref, cluster, "runs_in", "kubernetes")
                for pod in pods:
                    ref = node(
                        "pod", f"{cluster.external_id}:{pod.metadata.uid}", pod.metadata.name
                    )
                    link(service, ref, "has_pod", "kubernetes")
                    link(ref, cluster, "runs_in", "kubernetes")
                    for container in pod.spec.containers:
                        image = node("image", container.image)
                        link(ref, image, "uses_image", "kubernetes")
                        # 版本只声称镜像明确携带的 tag/digest，不推断与 Git revision 相同。
                        tail = container.image.rsplit("/", 1)[-1]
                        version = (
                            container.image.rsplit("@", 1)[1]
                            if "@" in container.image
                            else tail.rsplit(":", 1)[1]
                            if ":" in tail
                            else None
                        )
                        if version is not None:
                            link(
                                service,
                                node("version", container.image, version),
                                "runs_version",
                                "kubernetes",
                            )
            start = end - timedelta(seconds=lookback_seconds)
            traces = await self.arms.query_traces(
                Window(service_name=app.service_name, start=start, end=end)
            )
            for trace in traces:
                if trace.service_name != app.service_name:
                    raise ValueError("ARMS 返回其他服务的 Trace")
                spans = {
                    span.span_id: span for span in trace.spans if start <= span.timestamp < end
                }
                for edge in topology((trace,)):
                    if edge.parent_span_id not in spans or edge.span_id not in spans:
                        continue
                    if edge.source_service != edge.target_service:
                        link(
                            node("service", edge.source_service),
                            node("service", edge.target_service),
                            "calls",
                            "arms",
                            0.9,
                            spans[edge.span_id].timestamp,
                        )
            if app.service_name in self.git_services:
                repository = await self.git.get_repository(app.service_name)
                if repository.service_name != app.service_name:
                    raise ValueError("Git 返回其他服务的仓库")
                link(
                    service,
                    node("repository", repository.source_ref, repository.name),
                    "repository",
                    repository.source,
                )
            else:
                missing.append(f"git:{app.service_name}")
            if app.service_name in self.cloud_services:
                resources = await self.cloud.get_cloud_resources(
                    CloudQuery(service_name=app.service_name, start=start, end=end)
                )
                targets: dict[tuple[str, str], NodeRef] = {}
                for resource in resources.resources:
                    if resource.service_name != app.service_name:
                        raise ValueError("云资源不属于当前服务")
                    kind = {"rds": "database", "redis": "cache"}.get(
                        resource.product, "cloud_" + resource.product
                    )
                    ref = node(kind, resource.source_ref, resource.resource_id)
                    link(service, ref, "associated_resource", "alibaba_cloud")
                    if resource.product == "mq":
                        targets[resource.region_id, resource.resource_id] = ref
                for topic in await self.cloud.list_topics(app.service_name):
                    target = targets.get((topic.region_id, topic.instance_id))
                    if target is None:
                        raise ValueError("Topic 没有已发现的 MQ 实例")
                    link(
                        target,
                        node(
                            "topic",
                            f"alibaba:mq:{topic.region_id}:{topic.instance_id}:{topic.name}",
                            topic.name,
                        ),
                        "contains_topic",
                        "alibaba_cloud",
                    )
            else:
                missing.append(f"cloud:{app.service_name}")
        return DiscoverySnapshot(
            nodes=tuple(nodes[key] for key in sorted(nodes)),
            relations=tuple(relations[key] for key in sorted(relations)),
            missing_bindings=tuple(sorted(missing)),
        )


async def configured_sources(
    stack: AsyncExitStack, settings: Settings, end: datetime
) -> DiscoverySources:
    ops = await stack.enter_async_context(create_ops_platform_connector(settings))
    k8s = await stack.enter_async_context(create_kubernetes_connector(settings))
    # 专用可移动时间窗 Fake，明确是样例；不改变已有可观测性验收快照。
    arms = await stack.enter_async_context(
        FakeARMSConnector(discovery_traces(end))
        if settings.connector_mode.value == "fake"
        else create_arms_connector(settings)
    )
    git = await stack.enter_async_context(create_git_connector(settings))
    cloud = await stack.enter_async_context(create_cloud_connector(settings))
    return DiscoverySources(
        ops,
        k8s,
        arms,
        git,
        cloud,
        frozenset({"payment-service"})
        if settings.connector_mode.value == "fake"
        else frozenset(settings.require_git_config().services),
        frozenset({"payment-service"})
        if settings.connector_mode.value == "fake"
        else frozenset(settings.require_cloud_config().services),
    )
