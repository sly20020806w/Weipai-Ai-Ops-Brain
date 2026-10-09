"""三个 L0 高级 Tool；调用仍经既有 Dispatcher、Policy、Ledger 与审计。"""

from app.connectors.kubernetes.client import KubernetesConnector
from app.connectors.kubernetes.models import Deployment, Event, Namespace, Pod, ServiceName
from app.policy.models import RiskLevel
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class KubernetesQuery(ToolModel):
    namespace: Namespace
    service_name: ServiceName | None = None


class RuntimeQuery(ToolModel):
    namespace: Namespace
    service_name: ServiceName


class KubernetesStatus(ToolModel):
    cluster_name: str
    namespace: Namespace
    deployments: tuple[Deployment, ...]


class ServiceRuntime(ToolModel):
    cluster_name: str
    namespace: Namespace
    service_name: ServiceName
    pods: tuple[Pod, ...]


class KubernetesEvents(ToolModel):
    cluster_name: str
    namespace: Namespace
    events: tuple[Event, ...]


def register_kubernetes_tools(registry: ToolRegistry, connector: KubernetesConnector) -> None:
    async def get_status(query: KubernetesQuery) -> KubernetesStatus:
        return KubernetesStatus(
            cluster_name=connector.cluster_name,
            namespace=query.namespace,
            deployments=await connector.list_deployments(
                query.namespace, service_name=query.service_name
            ),
        )

    async def get_runtime(query: RuntimeQuery) -> ServiceRuntime:
        return ServiceRuntime(
            cluster_name=connector.cluster_name,
            namespace=query.namespace,
            service_name=query.service_name,
            pods=await connector.list_pods(query.namespace, service_name=query.service_name),
        )

    async def query_events(query: KubernetesQuery) -> KubernetesEvents:
        return KubernetesEvents(
            cluster_name=connector.cluster_name,
            namespace=query.namespace,
            events=await connector.list_events(query.namespace, service_name=query.service_name),
        )

    registry.register(
        name="get_k8s_status",
        description="读取指定命名空间/服务的 Deployment 副本和状态",
        input_model=KubernetesQuery,
        output_model=KubernetesStatus,
        handler=get_status,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="get_service_runtime",
        description="读取指定服务的 Pod、容器镜像、Ready 和重启次数",
        input_model=RuntimeQuery,
        output_model=ServiceRuntime,
        handler=get_runtime,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="query_events",
        description="读取 Kubernetes Event；按服务查询时匹配现存 Deployment/Pod UID",
        input_model=KubernetesQuery,
        output_model=KubernetesEvents,
        handler=query_events,
        risk_level=RiskLevel.L0,
    )
