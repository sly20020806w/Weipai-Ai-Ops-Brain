"""Discovery 的确定性输入和最小关系快照，不携带凭证或原始遥测。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AwareDatetime, Field, StringConstraints, field_validator

from app.tools.models import ToolModel

Text = Annotated[str, StringConstraints(min_length=1, max_length=300, strip_whitespace=True)]


class NodeRef(ToolModel):
    kind: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    external_id: Text
    name: Text

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.external_id


class Relation(ToolModel):
    origin: NodeRef
    target: NodeRef
    relation: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    source: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    observed_at: AwareDatetime

    @field_validator("observed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class DiscoverySnapshot(ToolModel):
    nodes: tuple[NodeRef, ...]
    relations: tuple[Relation, ...]
    missing_bindings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryInput:
    lookback_seconds: int = 900
    activity_timeout_seconds: int = 300
    activity_max_attempts: int = 3


@dataclass(frozen=True)
class DiscoveryRequest:
    observed_at: str
    lookback_seconds: int


@dataclass(frozen=True)
class DiscoveryResult:
    observed_at: str
    node_count: int
    edge_count: int
    missing_bindings: tuple[str, ...]
