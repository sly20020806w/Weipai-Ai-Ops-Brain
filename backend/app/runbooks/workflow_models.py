"""Temporal Runbook 匹配的冻结快照；重试不重新选择后来修改的步骤。"""

import hashlib
import json
from dataclasses import dataclass

from app.tasks.workflow_models import TaskSnapshot


def spec_hash(spec_json: str) -> str:
    return hashlib.sha256(
        json.dumps(json.loads(spec_json), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class RunbookMatchRequest:
    task: TaskSnapshot
    spec_json: str


@dataclass(frozen=True)
class RunbookMatchResult:
    runbook_json: str | None
    reason: str
    search_evidence_id: str | None
    blocked: bool = False
