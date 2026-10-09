"""反证覆盖、证据引用与宿主计算的置信度；模型不能自行放行任务。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.agent.investigation import AgentConclusion, EvidenceClaim, InvestigationSpec
from app.agent.models import ToolCall
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import JsonObject, ToolModel

REVIEW_TOOLS = frozenset({"query_metrics", "query_logs", "query_traces", "get_recent_changes"})
REVIEW_ACTOR = "codex-reviewer"


def within_review_scope(call: ToolCall, spec: InvestigationSpec) -> bool:
    values = call.function.parsed_arguments
    if values.get("service_name") != spec.service_name:
        return False
    try:
        end = datetime.fromisoformat(str(values["end"]))
        if call.function.name == "get_recent_changes":
            lookback = values.get("lookback_seconds")
            return (
                end == spec.end
                and type(lookback) is int
                and 0 < lookback <= (spec.end - spec.start).total_seconds()
            )
        start = datetime.fromisoformat(str(values["start"]))
        return spec.start <= start < end <= spec.end
    except (ValueError, TypeError, KeyError):
        return False


class AlternativeCause(StrEnum):
    NETWORK = "network"
    REDIS = "redis"
    RELEASE = "release"
    THIRD_PARTY = "third_party"


class ReviewCheck(EvidenceClaim):
    alternative: AlternativeCause
    outcome: Literal["not_supported", "contradicts", "inconclusive"]


class ReviewReport(ToolModel):
    checks: Annotated[tuple[ReviewCheck, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def complete_coverage(self) -> "ReviewReport":
        if {item.alternative for item in self.checks} != set(AlternativeCause):
            raise ValueError("必须分别反证网络、Redis、发布与第三方依赖")
        return self

    @property
    def evidence_ids(self) -> frozenset[UUID]:
        return frozenset(reference for item in self.checks for reference in item.evidence_ids)

    @property
    def verdict(self) -> str:
        if any(item.outcome == "contradicts" for item in self.checks):
            return "contradicted"
        if any(item.outcome == "inconclusive" for item in self.checks):
            return "inconclusive"
        return "clear"


class ReviewInput(ToolModel):
    spec: InvestigationSpec
    conclusion_evidence_id: UUID
    conclusion: AgentConclusion
    # 主 Agent 原始引用快照是待复核的数据，而非 Reviewer 查询产生的独立证据。
    evidence_snapshots: tuple[JsonObject, ...]


class ReviewDecision(ToolModel):
    conclusion_evidence_id: UUID
    original_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    conclusion: AgentConclusion
    report: ReviewReport
    observed_ids: Annotated[tuple[UUID, ...], Field(min_length=1)]
    steps: int = Field(ge=1, le=100)


def adjusted_conclusion(original: AgentConclusion, report: ReviewReport) -> AgentConclusion:
    # 置信度只由宿主按固定规则调整，不把模型主观评分当成执行授权。
    delta = 0.1 if report.verdict == "clear" else -0.2 if report.verdict == "contradicted" else 0
    return original.model_copy(
        update={"confidence": round(min(1.0, max(0.0, original.confidence + delta)), 10)}
    )


@dataclass(frozen=True)
class ReviewRequest:
    task: TaskSnapshot
    spec_json: str
    conclusion_evidence_id: str


@dataclass(frozen=True)
class ReviewResult:
    evidence_id: str
    decision_json: str
