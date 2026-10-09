"""专家角色与宿主固定的只读 Tool 白名单。"""

from enum import StrEnum
from types import MappingProxyType
from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from app.agent.investigation import EvidenceClaim, InvestigationSpec, Text
from app.tools.models import ToolModel


class ExpertKind(StrEnum):
    KUBERNETES = "Kubernetes"
    DATABASE = "Database"
    NETWORK = "Network"
    RELEASE = "Release"
    SECURITY = "Security"
    COST = "Cost"
    HOLMESGPT = "HolmesGPT"


TOOL_ALLOWLISTS = MappingProxyType(
    {
        ExpertKind.KUBERNETES: frozenset(
            {
                "get_service_context",
                "get_dependencies",
                "get_k8s_status",
                "get_service_runtime",
                "query_events",
                "query_metrics",
                "query_logs",
            }
        ),
        ExpertKind.DATABASE: frozenset(
            {
                "get_service_context",
                "get_dependencies",
                "get_cloud_resources",
                "query_metrics",
                "query_logs",
                "query_traces",
            }
        ),
        ExpertKind.NETWORK: frozenset(
            {
                "get_service_context",
                "get_dependencies",
                "get_cloud_resources",
                "query_metrics",
                "query_traces",
            }
        ),
        ExpertKind.RELEASE: frozenset(
            {
                "get_service_context",
                "get_recent_changes",
                "get_recent_deployments",
                "compare_versions",
                "get_service_runtime",
                "query_metrics",
                "query_logs",
            }
        ),
        ExpertKind.SECURITY: frozenset(
            {
                "get_service_context",
                "get_dependencies",
                "get_recent_changes",
                "get_k8s_status",
                "query_events",
                "query_logs",
            }
        ),
        ExpertKind.COST: frozenset(
            {
                "get_service_context",
                "get_cloud_resources",
                "query_metrics",
            }
        ),
        # Holmes 只分析已采集快照，不能执行源生工具或递归调用专家。
        ExpertKind.HOLMESGPT: frozenset(),
    }
)


class ExpertRequest(InvestigationSpec):
    expert: ExpertKind
    question: Text
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=30)] = ()

    @model_validator(mode="after")
    def unique_ids(self) -> "ExpertRequest":
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("专家输入证据不能重复")
        return self


class ExpertOpinion(ToolModel):
    assessment: EvidenceClaim
    findings: Annotated[tuple[EvidenceClaim, ...], Field(max_length=20)] = ()
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    uncertainties: Annotated[tuple[Text, ...], Field(max_length=20)] = ()

    @property
    def evidence_ids(self) -> frozenset[UUID]:
        return frozenset(
            value for claim in (self.assessment, *self.findings) for value in claim.evidence_ids
        )


class ExpertAdvice(ToolModel):
    expert: ExpertKind
    opinion: ExpertOpinion
    observed_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=30)]
    steps: int = Field(ge=1, le=30)

    @model_validator(mode="after")
    def references(self) -> "ExpertAdvice":
        if not self.opinion.evidence_ids <= set(self.observed_ids):
            raise ValueError("专家意见引用了未观察证据")
        return self
