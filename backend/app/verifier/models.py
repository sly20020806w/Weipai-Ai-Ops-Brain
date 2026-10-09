"""宿主提供的验证目标和阈值；验证不使用 LLM 判断或执行任何动作。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from app.connectors.observability.models import Window
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import ToolModel

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class ResourceExpectation(ToolModel):
    product: Text
    region_id: Text
    resource_id: Text
    healthy_status: Text


class VerificationConfig(ToolModel):
    max_5xx_ratio: float = Field(default=0.01, ge=0, lt=1, allow_inf_nan=False)
    max_p99_ms: float = Field(default=500.0, gt=0, allow_inf_nan=False)
    min_success_ratio: float = Field(default=0.99, gt=0, le=1, allow_inf_nan=False)
    max_rds_connection_ratio: float = Field(default=0.8, gt=0, le=1, allow_inf_nan=False)
    step_seconds: int = Field(default=60, ge=1, le=3600)
    min_metric_points: int = Field(default=3, ge=2, le=1000)
    max_sample_gap_seconds: int = Field(default=120, ge=1, le=3600)
    window_seconds: int = Field(default=300, ge=60, le=86400)
    resources_by_service: dict[str, tuple[ResourceExpectation, ...]] = Field(default_factory=dict)

    @field_validator("resources_by_service", mode="before")
    @classmethod
    def resource_arrays(cls, value: object) -> object:
        # pydantic-settings 将环境 JSON 先解码为 Python；只规范数组容器，仍严格验证各字段。
        if isinstance(value, dict):
            return {
                key: tuple(items) if isinstance(items, list) else items
                for key, items in value.items()
            }
        return value

    @model_validator(mode="after")
    def gap(self) -> "VerificationConfig":
        if self.max_sample_gap_seconds < self.step_seconds:
            raise ValueError("验证允许的采样间隔不能小于查询采样间隔")
        if self.window_seconds < self.step_seconds * (self.min_metric_points - 1):
            raise ValueError("验证窗口必须覆盖最少采样点")
        return self


def criteria_hash(config: VerificationConfig) -> str:
    """证据只保存判定配置的指纹，绝不把环境变量配置对象写入数据库。"""
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class VerificationSpec(Window, ToolModel):
    task_id: UUID
    verifying_version: int = Field(ge=1)
    action_id: Text
    action_completed_at: AwareDatetime
    cluster_name: Text
    namespace: Text
    deployment_name: Text
    container_name: Text
    expected_image: Text
    expected_replicas: int = Field(ge=1, le=10000)
    resources: Annotated[tuple[ResourceExpectation, ...], Field(min_length=1, max_length=100)]

    @field_validator("action_completed_at")
    @classmethod
    def utc_action_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def after_action(self) -> "VerificationSpec":
        if self.action_completed_at > self.start:
            raise ValueError("验证窗口必须在动作完成之后")
        identities = [(r.product, r.region_id, r.resource_id) for r in self.resources]
        if len(set(identities)) != len(identities):
            raise ValueError("验证资源目标不能重复")
        return self


class VerificationCheck(ToolModel):
    name: Text
    passed: bool
    reason: Text
    evidence_id: UUID | None = None


class VerificationReport(ToolModel):
    spec: VerificationSpec
    criteria_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    checks: Annotated[tuple[VerificationCheck, ...], Field(min_length=8, max_length=8)]

    @model_validator(mode="after")
    def complete_checks(self) -> "VerificationReport":
        if tuple(c.name for c in self.checks) != (
            "deployment",
            "pods",
            "http_5xx_ratio",
            "http_p99_ms",
            "http_success_ratio",
            "logs",
            "traces",
            "resources",
        ):
            raise ValueError("验证报告必须完整且按顺序包含八项独立检查")
        evidence_ids = [c.evidence_id for c in self.checks if c.evidence_id is not None]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("独立检查不能重复引用同一事实证据")
        return self

    @property
    def passed(self) -> bool:
        return all(check.passed and check.evidence_id is not None for check in self.checks)


@dataclass(frozen=True)
class VerificationRequest:
    task: TaskSnapshot
    spec_json: str


@dataclass(frozen=True)
class VerificationResult:
    task: TaskSnapshot
    evidence_id: str
    report_json: str
