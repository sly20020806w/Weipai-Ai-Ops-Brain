"""宿主熔断阈值和证据引用；模型不能通过布尔标记宣告熔断或复位。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import JsonObject, ToolModel


class SafetyConfig(ToolModel):
    max_consecutive_execution_failures: int = Field(default=3, ge=1, le=10)
    max_consecutive_runbook_failures: int = Field(default=2, ge=1, le=10)
    max_actions: int = Field(default=3, ge=1, le=100)
    min_metric_points: int = Field(default=3, ge=3, le=100)
    max_sample_gap_seconds: int = Field(default=120, ge=1, le=3600)
    max_5xx_ratio: float = Field(default=0.01, ge=0, lt=1, allow_inf_nan=False)
    max_p99_ms: float = Field(default=500, gt=0, allow_inf_nan=False)
    min_success_ratio: float = Field(default=0.99, gt=0, le=1, allow_inf_nan=False)
    min_5xx_increase: float = Field(default=0.02, gt=0, lt=1, allow_inf_nan=False)
    min_p99_increase_ms: float = Field(default=100, gt=0, allow_inf_nan=False)
    min_success_decrease: float = Field(default=0.02, gt=0, lt=1, allow_inf_nan=False)


class AbortReason(StrEnum):
    EXECUTION_FAILURES = "execution_failures"
    METRICS_WORSENING = "metrics_worsening"
    IMPACT_EXPANDING = "impact_expanding"
    EVIDENCE_CONFLICT = "evidence_conflict"
    RUNBOOK_FAILURES = "runbook_failures"
    ACTION_LIMIT = "action_limit"


REASON_TEXT = {
    AbortReason.EXECUTION_FAILURES: "连续操作失败",
    AbortReason.METRICS_WORSENING: "指标继续恶化",
    AbortReason.IMPACT_EXPANDING: "影响范围扩大",
    AbortReason.EVIDENCE_CONFLICT: "证据冲突",
    AbortReason.RUNBOOK_FAILURES: "Runbook 连续失败",
    AbortReason.ACTION_LIMIT: "超过最大 Action 次数",
}


class SafetyFinding(ToolModel):
    reason: AbortReason
    evidence_ids: tuple[UUID, ...] = ()
    audit_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class EvidenceFact:
    id: UUID
    source: str
    parameters: JsonObject
    result: JsonObject
    collected_at: datetime


@dataclass(frozen=True)
class AuditFact:
    id: UUID
    operation: str
    outcome: str
    occurred_at: datetime
    evidence_id: UUID | None
    details: JsonObject


@dataclass(frozen=True)
class SafetyResult:
    task: TaskSnapshot
    evidence_id: str | None = None
    reasons: tuple[str, ...] = ()


class AutomationAborted(PermissionError):
    pass


def config_hash(config: SafetyConfig) -> str:
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
