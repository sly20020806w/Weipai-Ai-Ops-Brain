"""L0 查询只读数据库，Policy、Evidence、审计与 Replay 复用统一 Dispatcher。"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.connectors.changes.models import ServiceName
from app.db.base import utc_now
from app.graph.changes.schemas import ChangeView
from app.graph.changes.service import TimelineService
from app.policy.models import RiskLevel
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class RecentChangesInput(ToolModel):
    service_name: ServiceName
    lookback_seconds: int = Field(default=3600, ge=1, le=2592000)
    end: AwareDatetime | None = None

    @field_validator("end")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class RecentChangesOutput(ToolModel):
    service_name: ServiceName
    start: AwareDatetime
    end: AwareDatetime
    history_scope: Literal["collected_source_history"] = "collected_source_history"
    events: tuple[ChangeView, ...]

    @model_validator(mode="after")
    def scope(self) -> "RecentChangesOutput":
        if self.start >= self.end or any(
            e.service_name != self.service_name or not self.start <= e.occurred_at < self.end
            for e in self.events
        ):
            raise ValueError("变更结果超出服务或时间范围")
        if len({e.identity for e in self.events}) != len(self.events):
            raise ValueError("变更结果重复")
        if tuple(e.occurred_at for e in self.events) != tuple(
            sorted(e.occurred_at for e in self.events)
        ):
            raise ValueError("变更结果必须按时间升序")
        return self


def register_timeline_tools(registry: ToolRegistry, timeline: TimelineService) -> None:
    async def recent(query: RecentChangesInput) -> RecentChangesOutput:
        end = query.end if query.end is not None else utc_now()
        start = end - timedelta(seconds=query.lookback_seconds)
        return RecentChangesOutput(
            service_name=query.service_name,
            start=start,
            end=end,
            events=await timeline.recent(query.service_name, start, end),
        )

    registry.register(
        name="get_recent_changes",
        description="按服务与 UTC 时间窗升序读取已采集变更引用",
        input_model=RecentChangesInput,
        output_model=RecentChangesOutput,
        handler=recent,
        risk_level=RiskLevel.L0,
    )
