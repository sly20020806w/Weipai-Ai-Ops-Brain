"""仅声明历史只读 schema；不构造 Connector，也没有 live 实现。"""

from app.agent.experts.models import ExpertAdvice, ExpertRequest
from app.learning.models import IncidentSearch
from app.policy.models import RiskLevel
from app.runbooks.schemas import RunbookSearch
from app.tools.changes import (
    CompareVersionsInput,
    CompareVersionsOutput,
    RecentDeploymentsInput,
    RecentDeploymentsOutput,
)
from app.tools.cloud import CloudResourcesInput, CloudResourcesOutput
from app.tools.graph import ContextQuery, DependencyQuery, GraphContext
from app.tools.incidents import SearchIncidentsOutput
from app.tools.kubernetes import (
    KubernetesEvents,
    KubernetesQuery,
    KubernetesStatus,
    RuntimeQuery,
    ServiceRuntime,
)
from app.tools.models import ToolModel
from app.tools.observability import (
    LogsOutput,
    MetricsInput,
    MetricsOutput,
    TracesOutput,
    WindowInput,
)
from app.tools.registry import ToolRegistry
from app.tools.runbooks import SearchRunbooksOutput
from app.tools.timeline import RecentChangesInput, RecentChangesOutput


def replay_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def forbidden(query: ToolModel) -> ToolModel:
        raise RuntimeError("Replay 注册表禁止执行任何 live 实现")

    schemas: tuple[tuple[str, type[ToolModel], type[ToolModel]], ...] = (
        ("get_service_context", ContextQuery, GraphContext),
        ("get_dependencies", DependencyQuery, GraphContext),
        ("get_recent_changes", RecentChangesInput, RecentChangesOutput),
        ("query_metrics", MetricsInput, MetricsOutput),
        ("query_logs", WindowInput, LogsOutput),
        ("query_traces", WindowInput, TracesOutput),
        ("get_k8s_status", KubernetesQuery, KubernetesStatus),
        ("get_service_runtime", RuntimeQuery, ServiceRuntime),
        ("query_events", KubernetesQuery, KubernetesEvents),
        ("get_cloud_resources", CloudResourcesInput, CloudResourcesOutput),
        ("compare_versions", CompareVersionsInput, CompareVersionsOutput),
        ("get_recent_deployments", RecentDeploymentsInput, RecentDeploymentsOutput),
        ("search_runbooks", RunbookSearch, SearchRunbooksOutput),
        ("search_incidents", IncidentSearch, SearchIncidentsOutput),
        ("consult_expert", ExpertRequest, ExpertAdvice),
    )
    for name, input_model, output_model in schemas:
        registry.register(
            name=name,
            description="仅按精确参数回放截止点前的已审计历史快照",
            input_model=input_model,
            output_model=output_model,
            handler=forbidden,
            risk_level=RiskLevel.L0,
        )
    return registry
