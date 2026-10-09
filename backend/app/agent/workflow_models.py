"""主 Agent 的 Temporal 纯数据请求。"""

from dataclasses import dataclass

from app.agent.investigation import InvestigationResult
from app.tasks.workflow_models import TaskSnapshot


@dataclass(frozen=True)
class InvestigationRequest:
    task: TaskSnapshot
    spec_json: str
    runbook_json: str | None = None
    review_evidence_id: str | None = None


@dataclass(frozen=True)
class ConclusionRequest:
    task: TaskSnapshot
    result: InvestigationResult
