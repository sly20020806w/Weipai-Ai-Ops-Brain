"""只读保障事实；原始监控留在源系统，仅传递测量与来源引用。"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.connectors.inspection.fake import sample_facts
from app.connectors.inspection.models import InspectionFacts
from app.connectors.kubernetes.models import ServiceName
from app.tools.models import ToolModel


class WarRoomQuery(ToolModel):
    service_name: ServiceName
    purpose: Literal["prepare", "watch", "cleanup", "verify"]
    window: int = Field(default=0, ge=0, le=1440)
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self) -> "WarRoomQuery":
        if self.end < self.start:
            raise ValueError("保障采集时间窗不能倒置")
        return self


class WarRoomFacts(ToolModel):
    query: WarRoomQuery
    inspection: InspectionFacts
    per_replica_rps: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    current_rps: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    rollback_ready: bool | None = None
    ready_replicas: int | None = Field(default=None, ge=0)
    capacity_reference: str = Field(pattern=r"^\S+$", max_length=512)
    observed_at: AwareDatetime

    @field_validator("observed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def scoped(self) -> "WarRoomFacts":
        from app.connectors.inspection.models import InspectionFact

        InspectionFact.safe_reference(self.capacity_reference)
        if self.inspection.service_name != self.query.service_name:
            raise ValueError("保障事实的服务范围不一致")
        return self


class WarRoomConnector(ABC):
    @abstractmethod
    async def query(self, value: WarRoomQuery) -> WarRoomFacts: ...


class FakeWarRoomConnector(WarRoomConnector):
    def __init__(self, *, abnormal_windows: frozenset[int] = frozenset()) -> None:
        self.abnormal_windows = abnormal_windows
        self.calls = 0

    async def query(self, value: WarRoomQuery) -> WarRoomFacts:
        value = WarRoomQuery.model_validate(value)
        self.calls += 1
        facts = sample_facts(value.service_name, abnormal=False)
        abnormal = value.purpose == "watch" and value.window in self.abnormal_windows
        facts = facts.model_copy(
            update={
                "facts": tuple(
                    f.model_copy(update={"value": False})
                    if abnormal
                    and f.check_id in {"service_health", "alerts_healthy", "logs_healthy"}
                    else f
                    for f in facts.facts
                )
            }
        )
        return WarRoomFacts(
            query=value,
            inspection=facts,
            per_replica_rps=250.0,
            current_rps=1000.0 if value.purpose == "watch" else 400.0,
            rollback_ready=True,
            ready_replicas=100,
            capacity_reference=f"fake://capacity/{value.service_name}",
            observed_at=datetime.now(UTC),
        )
