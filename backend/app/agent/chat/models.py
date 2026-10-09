"""每轮对话是一个 Human 任务；调用方不能提供操作人或执行权限。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from app.agent.investigation import InvestigationSpec, Text
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import ToolModel


class ChatInput(ToolModel):
    model_config = ConfigDict(strict=False)
    request_id: UUID
    service_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
    message: Text
    mode: Literal["question", "task"] = "question"
    previous_task_id: UUID | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def window(self) -> "ChatInput":
        if (self.start is None) != (self.end is None):
            raise ValueError("start/end 必须同时提供")
        if self.start is not None and self.end is not None:
            self.spec(self.start, self.end, 20)
        return self

    def spec(self, start: datetime, end: datetime, max_steps: int) -> InvestigationSpec:
        return InvestigationSpec(
            service_name=self.service_name,
            title=self.message,
            start=start,
            end=end,
            max_steps=max_steps,
        )


class ChatSubmission(ToolModel):
    input: ChatInput
    actor: Text


class ChatAnswer(ToolModel):
    task_id: UUID
    status: str
    pending: bool
    answer: str | None = None
    evidence_ids: tuple[UUID, ...] = ()
    conclusion_evidence_id: UUID | None = None
    review_evidence_id: UUID | None = None
    plan_evidence_id: UUID | None = None
    policy_decision: str | None = None


@dataclass(frozen=True)
class ChatVerifyRequest:
    task: TaskSnapshot
    conclusion_evidence_id: str


class ChatVerification(ToolModel):
    task_id: UUID
    verifying_version: int
    conclusion_evidence_id: UUID
    passed: bool
