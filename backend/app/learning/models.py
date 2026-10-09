"""第 22 节的十三个复盘章节；每条结论必须引用持久化 Evidence。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from app.agent.investigation import EvidenceClaim
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import ToolModel
from app.triggers.schemas import EventReceipt

SECTIONS = (
    "事件现象",
    "影响范围",
    "Timeline",
    "证据链",
    "根因",
    "处理过程",
    "验证结果",
    "为什么没有提前发现",
    "监控改进",
    "告警改进",
    "架构改进",
    "自动化建议",
    "Runbook变更",
)


class PostmortemSection(ToolModel):
    title: str
    conclusions: Annotated[tuple[EvidenceClaim, ...], Field(min_length=1, max_length=30)]


class PostmortemDraft(ToolModel):
    sections: Annotated[tuple[PostmortemSection, ...], Field(min_length=13, max_length=13)]
    improvements: Annotated[tuple[EvidenceClaim, ...], Field(min_length=1, max_length=10)]

    @model_validator(mode="after")
    def complete(self) -> "PostmortemDraft":
        if tuple(section.title for section in self.sections) != SECTIONS:
            raise ValueError("复盘必须按设计第 22 节顺序完整包含十三个章节")
        return self

    @property
    def evidence_ids(self) -> frozenset[UUID]:
        return frozenset(
            evidence_id
            for claim in (*self.improvements, *(c for s in self.sections for c in s.conclusions))
            for evidence_id in claim.evidence_ids
        )


class TimelineItem(ToolModel):
    occurred_at: AwareDatetime
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    evidence_id: UUID

    @field_validator("occurred_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class IncidentReport(PostmortemDraft):
    task_id: UUID
    service_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    timeline: Annotated[tuple[TimelineItem, ...], Field(min_length=1)]
    runbook_id: UUID
    improvement_task_ids: Annotated[tuple[UUID, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def ordered(self) -> "IncidentReport":
        if tuple(item.occurred_at for item in self.timeline) != tuple(
            sorted(item.occurred_at for item in self.timeline)
        ):
            raise ValueError("复盘 Timeline 必须使用 UTC 时间顺序")
        return self


class IncidentSearch(ToolModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    service_name: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class IncidentHit(ToolModel):
    evidence_id: UUID
    report: IncidentReport


@dataclass(frozen=True)
class LearningRequest:
    task: TaskSnapshot


@dataclass(frozen=True)
class LearningResult:
    evidence_id: str
    report_json: str
    improvements: list[EventReceipt]
