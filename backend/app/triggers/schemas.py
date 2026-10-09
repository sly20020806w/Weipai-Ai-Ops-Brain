"""统一事件只保存必要引用与摘要，不保存源系统原始日志。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.tasks.states import TaskSource

EventOrigin = Literal[
    "prometheus",
    "kubernetes",
    "ops_platform",
    "git",
    "ci",
    "argocd",
    "config_center",
    "cloud",
    "manual",
    "schedule",
    "state",
    "prediction",
    "learning",
]


class NormalizedEvent(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, revalidate_instances="always"
    )

    origin: EventOrigin
    source: TaskSource
    external_id: str = Field(min_length=1, max_length=512)
    service_name: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=500)
    occurred_at: AwareDatetime

    @field_validator("external_id", "service_name", "title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("事件文本不能为空、带首尾空白或控制字符")
        return value

    @field_validator("occurred_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def fingerprint(self) -> str:
        # 外部事件 ID 在来源内唯一；重新关联服务、改摘要不会产生第二个任务。
        import json

        identity = [self.origin, self.source.value, self.external_id]
        return sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class EventBatch:
    events: list[str]


@dataclass(frozen=True)
class EventReceipt:
    event_id: str
    task_id: str
    workflow_id: str
    duplicate: bool


@dataclass(frozen=True)
class WatchInput:
    namespace: str
    resource_version: str = ""


@dataclass(frozen=True)
class WatchResult:
    events: list[str]
    resource_version: str
