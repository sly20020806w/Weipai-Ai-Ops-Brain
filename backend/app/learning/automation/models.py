"""统计范围、来源引用和建议；运行配置只从环境读取。"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from app.tools.models import ToolModel
from app.triggers.schemas import EventReceipt

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


class WorkKind(StrEnum):
    TICKET = "ticket"
    INCIDENT = "incident"
    RUNBOOK = "runbook"
    RELEASE_CHECK = "release_check"
    MANUAL = "manual"


LABELS = {
    WorkKind.TICKET: "重复工单",
    WorkKind.INCIDENT: "重复故障",
    WorkKind.RUNBOOK: "重复 Runbook",
    WorkKind.RELEASE_CHECK: "重复发布检查",
    WorkKind.MANUAL: "重复人工操作",
}
METHODS = {
    WorkKind.TICKET: "workflow",
    WorkKind.INCIDENT: "automatic_runbook",
    WorkKind.RUNBOOK: "self_healing",
    WorkKind.RELEASE_CHECK: "workflow",
    WorkKind.MANUAL: "script",
}
METHOD_LABELS = {
    "workflow": "评估 Workflow 化",
    "automatic_runbook": "评估自动 Runbook",
    "self_healing": "评估 Self-Healing",
    "script": "评估脚本化",
}


class AutomationConfig(ToolModel):
    enabled: bool = True
    threshold: int = Field(default=5, ge=2, le=10000)
    lookback_seconds: int = Field(default=2592000, ge=1, le=31536000)
    interval_seconds: int = Field(default=3600, ge=1, le=86400)
    schedule_id: str = Field(
        default="weipai-automation-discovery", pattern=r"^[a-z][a-z0-9-]{0,95}$"
    )
    activity_timeout_seconds: int = Field(default=120, ge=1, le=600)
    activity_max_attempts: int = Field(default=3, ge=1, le=10)


class ScanWindow(ToolModel):
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self) -> "ScanWindow":
        if self.start >= self.end:
            raise ValueError("扫描窗口必须为 UTC 半开区间")
        return self


class WorkRecord(ToolModel):
    kind: WorkKind
    service_name: Name
    signature: Text
    table: Literal["ops_events", "evidence_ledger", "audit_log"]
    record_id: UUID
    task_id: UUID
    occurred_at: AwareDatetime
    evidence_id: UUID | None = None

    @field_validator("occurred_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def group_key(self) -> str:
        identity = [self.kind.value, self.service_name, " ".join(self.signature.split()).casefold()]
        return sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


class Repetition(ToolModel):
    group_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: Annotated[tuple[WorkRecord, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def consistent(self) -> "Repetition":
        if any(r.group_key != self.group_key for r in self.records):
            raise ValueError("重复劳动分组不能混入其他服务或类型")
        if len({(r.table, r.record_id) for r in self.records}) != len(self.records):
            raise ValueError("同一原始记录不能重复计数")
        return self


def group_records(records: tuple[WorkRecord, ...], threshold: int) -> tuple[Repetition, ...]:
    AutomationConfig(threshold=threshold)
    groups: dict[str, dict[tuple[str, UUID], WorkRecord]] = {}
    seen: dict[tuple[str, UUID], WorkRecord] = {}
    for record in records:
        record = WorkRecord.model_validate(record)
        previous = seen.get((record.table, record.record_id))
        if previous is not None and previous != record:
            raise ValueError("同一原始记录的统计快照冲突")
        seen[(record.table, record.record_id)] = record
        groups.setdefault(record.group_key, {})
        groups[record.group_key][(record.table, record.record_id)] = record
    return tuple(
        Repetition(
            group_key=key,
            records=tuple(sorted(items.values(), key=lambda r: (r.occurred_at, str(r.record_id)))),
        )
        for key, items in sorted(groups.items())
        if len(items) >= threshold
    )


class ManualOperation(ToolModel):
    task_id: UUID
    record_key: Name
    service_name: Name
    operation: Name
    actor: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    source_reference: Text
    occurred_at: AwareDatetime

    @field_validator("occurred_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class Suggestion(ToolModel):
    repetition: Repetition
    method: Literal["script", "workflow", "automatic_runbook", "self_healing"]
    conclusion: Text
    execution_requirement: str = (
        "这是自动化评估建议；实施仍须适用性审核、Policy、必要审批、Executor 和独立 Verifier。"
        "涉及业务判断时保留人工判断，Self-Healing 须满足既有成熟度及权限规则。"
    )


@dataclass(frozen=True)
class AutomationInput:
    activity_timeout_seconds: int = 120
    activity_max_attempts: int = 3


@dataclass(frozen=True)
class ScanRequest:
    end: str


@dataclass(frozen=True)
class AutomationResult:
    scanned_records: int
    receipts: list[EventReceipt]
    evidence_ids: list[str]
