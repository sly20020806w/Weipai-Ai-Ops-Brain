"""只保存测量值与来源引用，不复制原始指标、日志或凭证。"""

from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.connectors.kubernetes.models import ServiceName
from app.tools.models import ToolModel


class InspectionQuery(ToolModel):
    service_name: ServiceName


class InspectionFact(ToolModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    resource: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    value: bool | float | None
    source_reference: str = Field(min_length=1, max_length=512, pattern=r"^\S+$")
    observed_at: AwareDatetime

    @field_validator("source_reference")
    @classmethod
    def safe_reference(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not parsed.scheme
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("事实引用必须为无凭证或查询参数的来源 URI")
        return value

    @field_validator("value")
    @classmethod
    def finite(cls, value: bool | float | None) -> bool | float | None:
        import math

        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("巡检测量值必须有限")
        return value

    @field_validator("observed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class InspectionFacts(ToolModel):
    service_name: ServiceName
    # complete=false 表示分页、范围或采集未完成，不能据此清除已有风险。
    complete: bool
    facts: tuple[InspectionFact, ...] = Field(max_length=5000)

    @model_validator(mode="after")
    def unique(self) -> "InspectionFacts":
        if len({(f.check_id, f.resource) for f in self.facts}) != len(self.facts):
            raise ValueError("同一检查和资源不能有冲突或重复观测")
        return self
