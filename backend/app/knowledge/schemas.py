"""只保存机器无法自动获得的知识；有效期使用 UTC 半开区间。"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.base import UTCDateTime, utc_now


class KnowledgeType(StrEnum):
    BUSINESS_RULE = "business_rule"
    STANDARD = "standard"
    SOP = "sop"
    EXPERIENCE = "experience"
    CONSTRAINT = "constraint"
    TEAM_CONVENTION = "team_convention"
    BUSINESS_PRIORITY = "business_priority"


class KnowledgeSchema(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", revalidate_instances="always"
    )


class KnowledgeDraft(KnowledgeSchema):
    kind: KnowledgeType
    content: Annotated[str, Field(min_length=1, max_length=20000)]
    source: Annotated[str, Field(min_length=1, max_length=512)]
    valid_from: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None

    @field_validator("content", "source")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("知识内容与来源不可为空白")
        return value

    @field_validator("valid_from", "expires_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return UTCDateTime.normalize(value)

    @model_validator(mode="after")
    def ordered_validity(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("有效期结束必须晚于开始")
        return self


class KnowledgeView(KnowledgeDraft):
    id: UUID
    created_at: datetime
    updated_at: datetime
    embedding_model: str
    embedding_dimensions: int


class KnowledgeSearch(KnowledgeSchema):
    query: Annotated[str, Field(min_length=1, max_length=20000)]
    limit: Annotated[int, Field(ge=1, le=100)] = 10
    kind: KnowledgeType | None = None
    at: datetime = Field(default_factory=utc_now)

    @field_validator("query")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("查询不可为空白")
        return value

    @field_validator("at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        result = UTCDateTime.normalize(value)
        assert result is not None
        return result


class KnowledgePage(KnowledgeSchema):
    kind: KnowledgeType | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0)] = 0


class KnowledgeMatch(KnowledgeSchema):
    entry: KnowledgeView
    similarity: Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
