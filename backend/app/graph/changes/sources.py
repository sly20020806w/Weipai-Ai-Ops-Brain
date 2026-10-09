"""六类来源仅经只读 Connector 采集；完整快照校验后才进入数据库事务。"""

import hashlib
from contextlib import AsyncExitStack
from dataclasses import dataclass

from app.config import Settings
from app.connectors.changes.base import (
    ArgoCDConnector,
    CIConnector,
    ConfigCenterConnector,
    GitConnector,
)
from app.connectors.changes.factory import (
    create_argocd_connector,
    create_ci_connector,
    create_config_center_connector,
    create_git_connector,
)
from app.connectors.changes.models import DeploymentQuery
from app.connectors.cloud.base import CloudConnector
from app.connectors.cloud.factory import create_cloud_connector
from app.connectors.cloud.models import CloudQuery
from app.connectors.kubernetes.client import KubernetesConnector
from app.connectors.kubernetes.factory import create_kubernetes_connector
from app.connectors.kubernetes.fake import FakeKubernetesConnector, timeline_snapshot
from app.graph.changes.schemas import ChangeFact, ChangeSnapshot


def scoped_ref(value: str) -> str:
    return value if len(value) <= 512 else "sha256:" + hashlib.sha256(value.encode()).hexdigest()


@dataclass
class TimelineSources:
    git: GitConnector
    ci: CIConnector
    argocd: ArgoCDConnector
    config: ConfigCenterConnector
    k8s: KubernetesConnector
    cloud: CloudConnector
    bindings: dict[str, frozenset[str]]

    async def collect(self, query: DeploymentQuery) -> ChangeSnapshot:
        query = DeploymentQuery.model_validate(query)
        events: list[ChangeFact] = []
        missing: list[str] = []

        def bound(name: str) -> bool:
            if query.service_name in self.bindings[name]:
                return True
            missing.append(f"{name}:{query.service_name}")
            return False

        def remember(data: dict[str, object]) -> None:
            fact = ChangeFact.model_validate(data)
            if fact.service_name != query.service_name or not query.contains(fact.occurred_at):
                raise ValueError("源变更超出服务或时间窗")
            events.append(fact)

        for name, reader in (("git", self.git), ("config_center", self.config)):
            if bound(name):
                for item in await reader.list_changes(query):
                    remember(
                        {
                            "service_name": item.service_name,
                            "source": item.source,
                            "kind": item.kind,
                            "source_ref": item.source_ref,
                            "occurred_at": item.timestamp,
                            "revision": item.revision,
                        }
                    )
        if bound("ci"):
            for build in await self.ci.list_builds(query):
                remember(
                    {
                        "service_name": build.service_name,
                        "source": build.source,
                        "kind": "Build",
                        "source_ref": build.source_ref,
                        "occurred_at": build.timestamp,
                        "revision": build.revision,
                    }
                )
        if bound("argocd"):
            for deploy in await self.argocd.list_deployments(query):
                remember(
                    {
                        "service_name": deploy.service_name,
                        "source": "argocd",
                        "kind": "Sync",
                        "source_ref": deploy.source_ref,
                        "occurred_at": deploy.timestamp,
                        "revision": deploy.revision,
                    }
                )
        for namespace in await self.k8s.list_namespaces():
            for event in await self.k8s.list_events(namespace, service_name=query.service_name):
                # 聚合 Event 的 lastTimestamp 会变化；同 UID 只保留首次发生事实。
                timestamp = event.first_timestamp or event.event_time
                if timestamp is None or not query.contains(timestamp):
                    continue
                kind = (
                    "Image"
                    if event.reason == "Pulled" and event.involved_object.kind == "Pod"
                    else "Deploy"
                    if event.reason == "NewReplicaSetAvailable"
                    and event.involved_object.kind == "Deployment"
                    else "KubernetesEvent"
                )
                remember(
                    {
                        "service_name": query.service_name,
                        "source": "kubernetes",
                        "kind": kind,
                        "source_ref": scoped_ref(
                            f"kubernetes:{self.k8s.cluster_name}:{namespace}:{event.metadata.uid}"
                        ),
                        "occurred_at": timestamp,
                    }
                )
        if bound("cloud"):
            resources = await self.cloud.get_cloud_resources(CloudQuery(**query.model_dump()))
            for cloud_event in resources.events:
                remember(
                    {
                        "service_name": cloud_event.service_name,
                        "source": "alibaba_cloud",
                        "kind": "CloudEvent",
                        "source_ref": scoped_ref(
                            f"alibaba:{cloud_event.region_id}:{cloud_event.product}:"
                            f"{cloud_event.resource_id}:{cloud_event.id}"
                        ),
                        "occurred_at": cloud_event.timestamp,
                    }
                )
        return ChangeSnapshot(
            **query.model_dump(),
            events=tuple(sorted(events, key=lambda e: (e.occurred_at, e.identity))),
            missing_bindings=tuple(sorted(missing)),
        )


async def configured_sources(stack: AsyncExitStack, settings: Settings) -> TimelineSources:
    fake = settings.connector_mode.value == "fake"
    k8s = await stack.enter_async_context(
        FakeKubernetesConnector(timeline_snapshot())
        if fake
        else create_kubernetes_connector(settings)
    )
    return TimelineSources(
        await stack.enter_async_context(create_git_connector(settings)),
        await stack.enter_async_context(create_ci_connector(settings)),
        await stack.enter_async_context(create_argocd_connector(settings)),
        await stack.enter_async_context(create_config_center_connector(settings)),
        k8s,
        await stack.enter_async_context(create_cloud_connector(settings)),
        {
            name: frozenset({"payment-service"})
            for name in ("git", "ci", "argocd", "config_center", "cloud")
        }
        if fake
        else {
            "git": frozenset(settings.require_git_config().services),
            "ci": frozenset(settings.require_ci_config().services),
            "argocd": frozenset(settings.require_argocd_config().services),
            "config_center": frozenset(settings.require_config_center_config().services),
            "cloud": frozenset(settings.require_cloud_config().services),
        },
    )
