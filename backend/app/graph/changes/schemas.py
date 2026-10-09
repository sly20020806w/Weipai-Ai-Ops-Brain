"""采集与查询的 UTC 最小事实契约。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, field_validator, model_validator

from app.connectors.changes.models import DeploymentQuery, Identifier, ServiceName
from app.tools.models import ToolModel

ChangeKind = Literal[
    "Commit", "Merge", "Build", "Image", "Sync", "Deploy", "Config", "KubernetesEvent", "CloudEvent"
]
ChangeSource = Literal[
    "gitlab",
    "github",
    "jenkins",
    "gitlab_ci",
    "argocd",
    "config_center",
    "kubernetes",
    "alibaba_cloud",
]


class ChangeFact(ToolModel):
    service_name: ServiceName
    source: ChangeSource
    kind: ChangeKind
    source_ref: Identifier
    occurred_at: AwareDatetime
    revision: Identifier | None = None

    @field_validator("occurred_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def source_kind(self) -> "ChangeFact":
        allowed = {
            "gitlab": {"Commit", "Merge"},
            "github": {"Commit", "Merge"},
            "jenkins": {"Build"},
            "gitlab_ci": {"Build"},
            "argocd": {"Sync"},
            "config_center": {"Config"},
            "kubernetes": {"Image", "Deploy", "KubernetesEvent"},
            "alibaba_cloud": {"CloudEvent"},
        }
        if self.kind not in allowed[self.source]:
            raise ValueError("变更类型与来源不符")
        return self

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return self.service_name, self.source, self.kind, self.source_ref


class ChangeView(ChangeFact):
    id: UUID
    collected_at: AwareDatetime


class ChangeSnapshot(DeploymentQuery, ToolModel):
    events: tuple[ChangeFact, ...]
    missing_bindings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def scoped(self) -> "ChangeSnapshot":
        if any(
            e.service_name != self.service_name or not self.contains(e.occurred_at)
            for e in self.events
        ):
            raise ValueError("变更超出服务或时间窗")
        if len({e.identity for e in self.events}) != len(self.events):
            raise ValueError("采集快照包含重复变更")
        return self


@dataclass(frozen=True)
class TimelineInput:
    service_name: str = "payment-service"
    lookback_seconds: int = 3600
    end: str | None = None
    activity_timeout_seconds: int = 300
    activity_max_attempts: int = 3


@dataclass(frozen=True)
class TimelineRequest:
    service_name: str
    start: str
    end: str


@dataclass(frozen=True)
class TimelineResult:
    service_name: str
    start: str
    end: str
    event_count: int
    inserted_count: int
    missing_bindings: tuple[str, ...]
