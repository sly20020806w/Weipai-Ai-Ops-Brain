"""回放请求、结果与人工基准；基准只用于评分，不进入模型输入。"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator

from app.agent.investigation import AgentConclusion, InvestigationSpec
from app.tools.models import ToolModel

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class EvaluationLabel(ToolModel):
    accepted_root_causes: tuple[Text, ...] = ()
    false_alert: bool | None = None
    evidence_ids: Annotated[tuple[UUID, ...], Field(min_length=1)]


class ReplayRequest(ToolModel):
    run_id: UUID
    task_id: UUID
    baseline_evidence_id: UUID
    cutoff: AwareDatetime
    investigation: InvestigationSpec
    candidate_version: Text
    max_steps: int = Field(default=20, ge=1, le=100)

    @field_validator("cutoff")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ReplayReport(ToolModel):
    run_id: UUID
    task_id: UUID
    candidate_version: Text
    cutoff: AwareDatetime
    baseline_evidence_id: UUID
    candidate: AgentConclusion | None
    baseline_root_cause: Text
    baseline_hit: bool | None
    candidate_hit: bool | None
    misjudged: bool | None
    label_evidence_id: UUID | None
    baseline_tool_calls: int = Field(ge=0)
    candidate_tool_calls: int = Field(ge=0)
    baseline_elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    candidate_elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    observed_evidence_ids: tuple[UUID, ...]
    status: str
    error_code: str | None


def root_cause_hit(statement: str, label: EvaluationLabel | None) -> bool | None:
    if label is None or not label.accepted_root_causes:
        return None

    # 人工维护明确的等价表述；不让被评模型自己判断自己是否正确。
    def normalized(value: str) -> str:
        return " ".join(value.split()).casefold()

    return normalized(statement) in {normalized(value) for value in label.accepted_root_causes}
