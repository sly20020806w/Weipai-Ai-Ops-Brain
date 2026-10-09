"""工单场景的配置、证据与 Temporal 数据契约。"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from app.agent.investigation import EvidenceClaim
from app.connectors.ops_platform.models import Identifier, PermissionRequest
from app.tasks.workflow_models import ApprovalPrompt, TaskSnapshot
from app.tools.models import ToolModel


class TicketCategory(StrEnum):
    SQL = "sql"
    PERMISSION = "permission"
    RESOURCE = "resource"
    CONFIGURATION = "configuration"
    CONSULTATION = "consultation"
    INCIDENT = "incident"
    RELEASE = "release"


class PermissionBinding(ToolModel):
    service_name: Identifier
    subject_id: Identifier
    resource: Identifier
    permission: Identifier
    requester_id: Identifier


class TicketConfig(ToolModel):
    enabled: bool = False
    credential_ttl_seconds: int = Field(default=60, ge=1, le=300)
    max_permission_seconds: int = Field(default=86400, ge=60, le=604800)
    bindings: tuple[PermissionBinding, ...] = ()


class Classification(ToolModel):
    category: TicketCategory
    rationale: EvidenceClaim


class TicketContext(ToolModel):
    ticket_id: Identifier
    service_name: Identifier
    category: TicketCategory
    request: PermissionRequest
    evidence_ids: tuple[UUID, ...]
    missing: tuple[str, ...]
    judgment: str | None = None


class TicketReview(ToolModel):
    task_id: UUID
    rca_version: int
    conclusion_evidence_id: UUID
    claim: EvidenceClaim
    verdict: str = Field(pattern=r"^(clear|contradicted|inconclusive)$")


@dataclass(frozen=True)
class TicketStageRequest:
    task: TaskSnapshot
    ticket_id: str


@dataclass(frozen=True)
class TicketAnalysisRequest:
    task: TaskSnapshot
    context_json: str
    runbook_json: str | None = None


@dataclass(frozen=True)
class TicketPlanRequest:
    task: TaskSnapshot
    context_json: str
    conclusion_evidence_id: str
    review_evidence_id: str


@dataclass(frozen=True)
class TicketExecutionRequest:
    task: TaskSnapshot
    plan_evidence_id: str
    approval: ApprovalPrompt | None
    action_index: int


@dataclass(frozen=True)
class TicketVerifyRequest:
    task: TaskSnapshot
    plan_evidence_id: str
    final: bool = False


@dataclass(frozen=True)
class TicketVerifyResult:
    task: TaskSnapshot
    evidence_id: str
    passed: bool
