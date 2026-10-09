"""工单动作的精确命令，不包含凭证。"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from app.connectors.ops_platform.models import Identifier
from app.tools.models import ToolModel


class PermissionGrant(ToolModel):
    subject_id: Identifier
    resource: Identifier
    permission: Identifier
    expires_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("权限申请必须提供非空业务理由")
        return value


class PermissionQuery(ToolModel):
    service_name: Identifier
    subject_id: Identifier
    resource: Identifier


class PermissionState(ToolModel):
    service_name: Identifier
    subject_id: Identifier
    resource: Identifier
    grant: PermissionGrant | None


class TicketCommand(ToolModel):
    execution_id: UUID
    task_id: UUID
    plan_evidence_id: UUID
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_id: Identifier
    name: Literal["grant_ticket_permission", "close_ticket"]
    service_name: Identifier
    ticket_id: Identifier
    expected_updated_at: AwareDatetime
    grant: PermissionGrant
    resolution: str | None = Field(default=None, min_length=1, max_length=20000)

    @field_validator("expected_updated_at")
    @classmethod
    def utc_version_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


def ticket_command_hash(command: TicketCommand) -> str:
    return hashlib.sha256(
        json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class TicketReceipt(ToolModel):
    execution_id: UUID
    command_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: AwareDatetime
    ticket_id: Identifier
    status: Identifier

    @field_validator("completed_at")
    @classmethod
    def utc_completion(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)
