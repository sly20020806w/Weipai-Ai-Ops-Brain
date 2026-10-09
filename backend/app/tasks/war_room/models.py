"""保障输入、证据评估与 Temporal 数据契约；配置不入库。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.connectors.kubernetes.models import ServiceName
from app.executor.models import ExecutionTarget
from app.tasks.inspection.models import CheckResult
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import ToolModel
from app.triggers.schemas import EventReceipt

SECTIONS = (
    "容量评估",
    "资源准备",
    "监控检查",
    "告警检查",
    "Runbook检查",
    "回滚检查",
    "风险扫描",
    "实时盯盘",
    "异常处理",
    "结束后资源回收",
    "保障报告",
)


class WarRoomConfig(ToolModel):
    enabled: bool = True
    interval_seconds: float = Field(default=60, ge=0.1, le=3600, allow_inf_nan=False)
    max_windows: int = Field(default=1440, ge=1, le=1440)
    capacity_margin: float = Field(default=1.25, ge=1, le=3, allow_inf_nan=False)


class WarRoomSubmission(ToolModel):
    request_id: UUID
    service_name: ServiceName
    title: str = Field(min_length=1, max_length=450)
    kind: Literal["event", "new_service", "migration", "major_release", "peak"]
    start: AwareDatetime
    end: AwareDatetime
    projected_rps: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("title")
    @classmethod
    def text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("保障名称不能为空")
        return value.strip()

    @model_validator(mode="after")
    def duration(self) -> "WarRoomSubmission":
        if not 0 < (self.end - self.start).total_seconds() <= 86400:
            raise ValueError("保障时长必须大于零且最多一天")
        return self

    @property
    def digest(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


class WarRoomAssessment(ToolModel):
    task_id: UUID
    phase_version: int
    purpose: Literal["prepare", "watch", "cleanup"]
    submission_hash: str
    config_hash: str
    assessed_at: AwareDatetime
    target: ExecutionTarget
    baseline: ExecutionTarget
    required_replicas: int
    runbook_evidence_id: UUID
    runbook_decisions: tuple[tuple[bool, str], ...]
    facts_evidence_id: UUID
    target_evidence_id: UUID
    checks: tuple[CheckResult, ...]
    capacity_known: bool
    safe: bool
    complete: bool
    anomaly: bool
    ownership_matches: bool
    receipts: tuple[EventReceipt, ...] = ()


class WarRoomReview(ToolModel):
    task_id: UUID
    phase_version: int
    assessment_evidence_id: UUID
    target_evidence_id: UUID
    facts_evidence_id: UUID
    alternatives: tuple[str, ...]
    clear: bool


class WarRoomVerification(ToolModel):
    task_id: UUID
    verifying_version: int
    assessment_evidence_id: UUID
    target_evidence_id: UUID
    facts_evidence_id: UUID
    passed: bool
    final: bool


@dataclass(frozen=True)
class WarRoomRequest:
    task: TaskSnapshot
    purpose: str = "prepare"
    window: int = 0
    start: str | None = None
    end: str | None = None


@dataclass(frozen=True)
class WarRoomVerifyRequest:
    task: TaskSnapshot
    assessment_evidence_id: str
    final: bool = False


@dataclass(frozen=True)
class WarRoomResult:
    evidence_id: str
    report_json: str
    receipts: list[EventReceipt]
    blocked: bool = False


@dataclass(frozen=True)
class WarRoomVerified:
    task: TaskSnapshot
    evidence_id: str
    passed: bool
