"""任务控制台 HTTP 与 Temporal 共用的数据契约。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from app.tasks.approval.models import ApprovalTicket
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.workflow_models import HumanPrompt, HumanWaitRequest


class ConsoleModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class Page[T](ConsoleModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class InteractionView(ConsoleModel):
    task_id: UUID
    status: TaskStatus
    status_version: int
    approval: ApprovalTicket | None = None
    approval_request_evidence_id: UUID | None = None
    question: HumanPrompt | None = None
    recovery: HumanWaitRequest | None = None
    recovery_question_id: UUID | None = None


class TaskView(ConsoleModel):
    id: UUID
    title: str
    source: TaskSource
    status: TaskStatus
    status_version: int
    created_at: datetime
    updated_at: datetime


class EventView(ConsoleModel):
    id: UUID
    task_id: UUID
    origin: str
    source: TaskSource
    external_id: str
    service_name: str
    title: str
    fingerprint: str
    occurred_at: datetime
    created_at: datetime


class EvidenceView(ConsoleModel):
    id: UUID
    task_id: UUID
    source_tool: str
    parameters: dict[str, JsonValue]
    result_snapshot: JsonValue | None
    source_reference: str | None
    collected_at: datetime
    created_at: datetime


class ToolCallView(ConsoleModel):
    id: UUID
    task_id: UUID
    actor: str
    operation: str
    outcome: str
    evidence_id: UUID | None
    details: dict[str, JsonValue]
    occurred_at: datetime


class HistoryView(ConsoleModel):
    id: UUID
    task_id: UUID
    sequence: int
    from_status: TaskStatus | None
    to_status: TaskStatus
    reason: str
    actor: str
    changed_at: datetime


class ApprovalInput(ConsoleModel):
    approval_id: UUID
    wait_version: int = Field(ge=1, strict=True)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected"]


AnswerText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class AnswerInput(ConsoleModel):
    question_id: UUID
    wait_version: int = Field(ge=1, strict=True)
    answer: AnswerText


class TakeoverInput(ConsoleModel):
    expected_version: int = Field(ge=0, strict=True)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]


class ControlCommand(ConsoleModel):
    task_id: UUID
    kind: Literal["approval", "judgment", "information", "takeover"]
    actor: str
    payload: dict[str, JsonValue]


class ControlReceipt(ConsoleModel):
    task_id: UUID
    operation_id: str
    evidence_id: UUID
    outcome: Literal["signaled", "taken_over"]
