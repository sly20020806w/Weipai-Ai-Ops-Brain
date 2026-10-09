"""发布检查、灰度目标和观测窗口；配置只由环境提供。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.agent.investigation import EvidenceClaim
from app.executor.models import ExecutionTarget
from app.policy.models import RiskLevel
from app.tasks.planning.models import Identifier
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import ToolModel

Purpose = Literal["canary", "promote", "pause", "rollback"]


class ReleaseConfig(ToolModel):
    enabled: bool = False
    canary_percent: int = Field(default=10, ge=1, lt=100)
    observation_seconds: float = Field(default=60, gt=0, le=3600, allow_inf_nan=False)
    max_5xx_ratio: float = Field(default=0.01, ge=0, lt=1, allow_inf_nan=False)
    max_p99_ms: float = Field(default=500, gt=0, allow_inf_nan=False)
    min_success_ratio: float = Field(default=0.99, gt=0, le=1, allow_inf_nan=False)


class DeployParameters(ToolModel):
    from_version: Identifier
    to_version: Identifier
    traffic_percent: int = Field(ge=1, le=100)


class ReleaseQuery(ToolModel):
    release_id: Identifier


class ReleaseManifest(ToolModel):
    release_id: Identifier
    service_name: Identifier
    from_version: Identifier
    to_version: Identifier
    sql: tuple[str, ...]
    resource_ready: bool
    monitoring_ready: bool
    rollback_ready: bool
    reference: str = Field(min_length=1)

    @model_validator(mode="after")
    def different_versions(self) -> "ReleaseManifest":
        if self.from_version == self.to_version:
            raise ValueError("发布必须改变版本")
        return self


def manifest_hash(value: ReleaseManifest) -> str:
    return hashlib.sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


class ReleaseWindow(ReleaseQuery):
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def window(self) -> "ReleaseWindow":
        if not 0 < (self.end - self.start).total_seconds() <= 3600:
            raise ValueError("发布观测窗口必须为 0–3600 秒")
        return self


class ReleaseObservation(ReleaseWindow):
    service_name: Identifier
    target: ExecutionTarget
    deployment_ready: bool
    pods_ready: bool
    http_5xx: tuple[float, ...]
    p99_ms: tuple[float, ...]
    success_ratio: tuple[float, ...]
    logs_healthy: bool
    traces_healthy: bool
    resources_healthy: bool

    @field_validator("http_5xx", "p99_ms", "success_ratio")
    @classmethod
    def finite_samples(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        import math

        if any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("指标必须是有限非负值")
        return values

    @model_validator(mode="after")
    def same_service(self) -> "ReleaseObservation":
        if self.target.service_name != self.service_name or any(
            v > 1 for v in (*self.http_5xx, *self.success_ratio)
        ):
            raise ValueError("发布观测服务或比例值不合法")
        return self


class ReleaseCheck(ToolModel):
    name: Literal["git_diff", "impact", "sql", "resources", "monitoring", "rollback"]
    passed: bool
    risk_level: RiskLevel = RiskLevel.L0
    claim: EvidenceClaim


class ReleaseAssessment(ToolModel):
    task_id: UUID
    phase_version: int
    manifest: ReleaseManifest
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: Purpose
    checks: tuple[ReleaseCheck, ...]
    canary_percent: int
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def complete_checks(self) -> "ReleaseAssessment":
        if (
            len(self.checks) != 6
            or {c.name for c in self.checks}
            != {"git_diff", "impact", "sql", "resources", "monitoring", "rollback"}
            or self.manifest_hash != manifest_hash(self.manifest)
        ):
            raise ValueError("发布检查必须覆盖六项，且材料指纹一致")
        return self

    @property
    def passed(self) -> bool:
        return len(self.checks) == 6 and all(c.passed for c in self.checks)


@dataclass(frozen=True)
class ReleaseStageRequest:
    task: TaskSnapshot
    release_id: str
    purpose: Purpose = "canary"


@dataclass(frozen=True)
class ReleasePlanRequest:
    task: TaskSnapshot
    assessment_evidence_id: str
    review_evidence_id: str | None = None


@dataclass(frozen=True)
class ReleaseObserveRequest:
    task: TaskSnapshot
    release_id: str
    start: str
    end: str
    plan_evidence_id: str
    final: bool = False


@dataclass(frozen=True)
class ReleaseObserveResult:
    task: TaskSnapshot
    evidence_id: str
    passed: bool
    report_json: str
