"""规则配置留在环境；数据库只保存规则指纹、风险与证据引用。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.connectors.kubernetes.models import ServiceName
from app.db.base import Base, UTCDateTime
from app.tasks.inspection.catalog import BY_ID, Area, Category, Mode, Outcome
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import ToolModel


class InspectionConfig(ToolModel):
    enabled: bool = True
    services: tuple[ServiceName, ...] = Field(
        default=("payment-service",), min_length=1, max_length=20
    )
    max_age_seconds: int = Field(default=900, ge=1, le=86400)
    thresholds: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid(self) -> "InspectionConfig":
        import math

        if len(set(self.services)) != len(self.services):
            raise ValueError("巡检范围不能重复")
        for key, value in self.thresholds.items():
            if (
                key not in BY_ID
                or BY_ID[key].operator not in {"min", "max"}
                or not math.isfinite(value)
            ):
                raise ValueError("巡检阈值必须对应数值检查且为有限值")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()


class CheckResult(ToolModel):
    service_name: ServiceName
    check_id: str
    resource: str
    area: Area
    category: Category
    label: str
    outcome: Outcome
    evidence_id: UUID | None
    source_reference: str | None
    observed_at: datetime | None
    assessed_at: datetime
    reason: str

    @field_validator("observed_at", "assessed_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return UTCDateTime.normalize(value)


class InspectionReport(ToolModel):
    task_id: UUID
    phase_version: int = Field(ge=1)
    mode: Mode
    rule_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: tuple[CheckResult, ...]
    runbook_evidence_ids: tuple[UUID, ...]

    @property
    def complete(self) -> bool:
        return bool(self.checks) and all(item.outcome != "unknown" for item in self.checks)


class RiskEntry(Base):
    __tablename__ = "inspection_risks"
    __table_args__ = (
        UniqueConstraint("risk_key"),
        CheckConstraint("risk_key ~ '^[0-9a-f]{64}$'", name="key_valid"),
        CheckConstraint(
            "category IN ('stability','capacity','security','cost')", name="category_valid"
        ),
        CheckConstraint("outcome IN ('abnormal','unknown')", name="outcome_valid"),
        CheckConstraint("episode >= 1 AND last_seen >= first_seen", name="observation_valid"),
    )
    risk_key: Mapped[str] = mapped_column(String(64), nullable=False)
    service_name: Mapped[str] = mapped_column(String(63), nullable=False)
    check_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    opening_evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_ledger.id"), nullable=False
    )
    latest_evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_ledger.id"), nullable=False
    )
    notification_evidence_id: Mapped[UUID | None] = mapped_column(ForeignKey("evidence_ledger.id"))


def risk_key(value: CheckResult) -> str:
    return hashlib.sha256(
        json.dumps(
            [value.service_name, value.check_id, value.resource], separators=(",", ":")
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class InspectionRequest:
    task: TaskSnapshot
    mode: Mode


@dataclass(frozen=True)
class InspectionResult:
    evidence_id: str
    report_json: str
    risk_ids: list[str]


@dataclass(frozen=True)
class InspectionVerifyRequest:
    task: TaskSnapshot
    report_evidence_id: str


class InspectionVerification(ToolModel):
    task_id: UUID
    verifying_version: int
    report_evidence_id: UUID
    passed: bool
