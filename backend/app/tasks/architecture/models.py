"""十二维评审、输入快照和 Temporal 纯数据契约。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from app.connectors.ops_platform.models import Identifier
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import ToolModel

DIMENSIONS = (
    "稳定性",
    "高可用",
    "容量",
    "Kubernetes",
    "云资源",
    "网络",
    "存储",
    "安全",
    "成本",
    "运维复杂度",
    "可观测性",
    "发布和回滚",
)
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class ReviewSubmission(ToolModel):
    request_id: UUID
    service_name: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=480)]
    proposal: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)
    ]

    @property
    def digest(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


class Citation(ToolModel):
    evidence_id: UUID
    quote: Text


class DimensionReview(ToolModel):
    dimension: str
    outcome: Literal["risk", "supported", "unknown"]
    finding: Text
    recommendation: Text
    citations: Annotated[tuple[Citation, ...], Field(min_length=1, max_length=10)]

    @model_validator(mode="after")
    def unique_citations(self) -> Self:
        if len({c.evidence_id for c in self.citations}) != len(self.citations):
            raise ValueError("同一维度不得重复引用 Evidence")
        return self


class ReviewDraft(ToolModel):
    dimensions: Annotated[tuple[DimensionReview, ...], Field(min_length=12, max_length=12)]

    @model_validator(mode="after")
    def complete(self) -> Self:
        if tuple(d.dimension for d in self.dimensions) != DIMENSIONS:
            raise ValueError("评审必须按设计第 27 节顺序覆盖全部十二个维度")
        return self


class ReviewSources(ToolModel):
    proposal: UUID
    runbooks: UUID
    context: UUID
    standards: UUID
    incidents: UUID

    @property
    def ids(self) -> frozenset[UUID]:
        return frozenset(self.model_dump().values())


class ArchitectureReport(ReviewDraft):
    task_id: UUID
    phase_version: int = Field(ge=1)
    submission_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_name: Identifier
    sources: ReviewSources
    reviewed_at: AwareDatetime

    @field_validator("reviewed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


@dataclass(frozen=True)
class ArchitectureRequest:
    task: TaskSnapshot


@dataclass(frozen=True)
class ArchitectureResult:
    evidence_id: str
    report_json: str | None
    blocked: bool = False


@dataclass(frozen=True)
class ArchitectureVerifyRequest:
    task: TaskSnapshot
    report_evidence_id: str


class ArchitectureVerification(ToolModel):
    task_id: UUID
    verifying_version: int = Field(ge=1)
    report_evidence_id: UUID
    passed: bool
