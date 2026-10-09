"""认知与运营控制台的输入和投影契约。"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from app.db.base import utc_now
from app.knowledge.schemas import KnowledgeDraft
from app.ledger.models import AuditEventType
from app.runbooks.schemas import RunbookContent
from app.tasks.console_models import ConsoleModel, EventView, EvidenceView, TaskView

Center = Literal[
    "releases", "tickets", "inspections", "war-rooms", "architecture-reviews", "automations"
]
AuditKind = AuditEventType | Literal["catalog_edit"]


class TimeWindow(ConsoleModel):
    start: AwareDatetime = Field(default_factory=lambda: utc_now() - timedelta(days=30))
    end: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("开始时间必须早于结束时间")
        return self


class KnowledgeInput(KnowledgeDraft):
    @model_validator(mode="before")
    @classmethod
    def wire(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return KnowledgeDraft.model_validate_json(json.dumps(value)).model_dump()
        return value


class RunbookInput(RunbookContent):
    @model_validator(mode="before")
    @classmethod
    def wire(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return RunbookContent.model_validate_json(json.dumps(value)).model_dump()
        return value


class ChangeEntryView(ConsoleModel):
    id: UUID
    service_name: str
    source: str
    kind: str
    source_ref: str
    revision: str | None
    occurred_at: datetime
    created_at: datetime


class ScenarioView(ConsoleModel):
    task: TaskView
    event: EventView


class ScenarioDetail(ScenarioView):
    # 每条快照保留真实 ID、来源及 UTC 时间，阶段未完成时不伪造报告。
    evidence: list[EvidenceView]


class RiskView(ConsoleModel):
    id: UUID
    service_name: str
    check_id: str
    resource: str
    category: str
    outcome: str
    active: bool
    episode: int
    first_seen: datetime
    last_seen: datetime
    cleared_at: datetime | None
    opening_evidence_id: UUID
    latest_evidence_id: UUID
    notification_evidence_id: UUID | None


class AuditView(ConsoleModel):
    id: UUID
    task_id: UUID | None
    event_type: AuditKind
    actor: str
    operation: str
    outcome: str
    evidence_id: UUID | None
    details: dict[str, JsonValue]
    occurred_at: datetime
