"""源系统的关系与工单快照；不建立第二套 CMDB。"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]
Label = Annotated[str, Field(min_length=1, max_length=512)]


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class SourceRecord(_SnapshotModel):
    id: Identifier


class ServiceTreeNode(SourceRecord):
    name: Label
    parent_id: Identifier | None = None


class Application(SourceRecord):
    service_name: Identifier
    name: Label
    business_id: Identifier
    owner_ids: tuple[Identifier, ...] = Field(min_length=1)


class Owner(SourceRecord):
    name: Label
    team: Label


class PermissionRequest(_SnapshotModel):
    subject_id: Identifier | None = None
    resource: Identifier | None = None
    permission: Identifier | None = None
    expires_at: AwareDatetime | None = None
    reason: Label | None = None

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class Ticket(SourceRecord):
    title: Label
    description: str = Field(max_length=100_000)
    service_name: Identifier
    status: Identifier
    requester_id: Identifier
    assignee_id: Identifier | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    permission_request: PermissionRequest | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    resolution: str | None = Field(default=None, max_length=20000, exclude_if=lambda v: v is None)

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_time_order(self) -> "Ticket":
        if self.updated_at < self.created_at:
            raise ValueError("工单更新时间不能早于创建时间")
        return self


class Page[Record: SourceRecord](_SnapshotModel):
    items: tuple[Record, ...]
    next_cursor: Identifier | None = None


class OpsPlatformSnapshot(_SnapshotModel):
    service_tree: tuple[ServiceTreeNode, ...]
    applications: tuple[Application, ...]
    owners: tuple[Owner, ...]
    tickets: tuple[Ticket, ...]

    @model_validator(mode="after")
    def validate_relations(self) -> "OpsPlatformSnapshot":
        for records in (self.service_tree, self.applications, self.owners, self.tickets):
            ids = [record.id for record in records]
            if len(set(ids)) != len(ids):
                raise ValueError("Fake 快照的源系统 ID 不能重复")
        names = [app.service_name for app in self.applications]
        if len(set(names)) != len(names):
            raise ValueError("Fake 快照的 service_name 不能重复")
        nodes = {node.id: node for node in self.service_tree}
        owners = {owner.id for owner in self.owners}
        for node in self.service_tree:
            visited = {node.id}
            parent_id = node.parent_id
            while parent_id is not None:
                if parent_id not in nodes or parent_id in visited:
                    raise ValueError("Fake 服务树必须具有有效、无环的父子关系")
                visited.add(parent_id)
                parent_id = nodes[parent_id].parent_id
        for app in self.applications:
            if app.business_id not in nodes or not set(app.owner_ids) <= owners:
                raise ValueError("Fake 应用的业务归属或负责人引用不存在")
        for ticket in self.tickets:
            if ticket.service_name not in names:
                raise ValueError("Fake 工单的服务引用不存在")
        return self
