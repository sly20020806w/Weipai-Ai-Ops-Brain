"""只保存检测游标及当前异常事件引用，原始序列保留在源系统。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime
from app.tasks.states import TaskSource
from app.triggers.schemas import EventReceipt


class DetectionCursor(Base):
    __tablename__ = "detection_cursors"
    __table_args__ = (
        UniqueConstraint("detector_key"),
        CheckConstraint("detector_key ~ '^[0-9a-f]{64}$'", name="detector_key_format"),
    )
    detector_key: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    active_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("ops_events.id"))


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")
    detector_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: TaskSource
    service_name: str
    observed_at: AwareDatetime
    breached: bool
    title: str
    source_reference: str
    summary: dict[str, JsonValue]

    @field_validator("source")
    @classmethod
    def detection_source(cls, value: TaskSource) -> TaskSource:
        if value not in {TaskSource.STATE, TaskSource.PREDICTION}:
            raise ValueError("检测器只生成 State 或 Prediction 任务")
        return value

    @field_validator("observed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        result = UTCDateTime.normalize(value)
        assert result is not None
        return result


@dataclass(frozen=True)
class DetectionInput:
    activity_timeout_seconds: int = 60
    activity_max_attempts: int = 3


@dataclass(frozen=True)
class DetectionRequest:
    observed_at: str


@dataclass(frozen=True)
class DetectionBatch:
    observed_at: str
    observations: list[str]


@dataclass(frozen=True)
class DetectionResult:
    observed_at: str
    checked: int
    receipts: list[EventReceipt]
