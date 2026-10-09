"""审批哈希覆盖完整计划；排序规范化抵御 JSONB 的对象键重排。"""

import hashlib
import json
import re
from uuid import UUID, uuid5

from pydantic import Field

from app.tasks.planning.models import ActionPlan
from app.tasks.workflow_models import ApprovalResponse, TaskSnapshot
from app.tools.models import ToolModel


def action_hash(plan: ActionPlan) -> str:
    checked = ActionPlan.model_validate_json(plan.model_dump_json())
    return hashlib.sha256(
        json.dumps(
            checked.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def approval_id(task: TaskSnapshot, plan_evidence_id: str, digest: str) -> str:
    return str(uuid5(UUID(task.task_id), f"approval:{task.version}:{plan_evidence_id}:{digest}"))


def validate_response(value: ApprovalResponse) -> None:
    for identity in (value.task_id, value.approval_id):
        if str(UUID(identity)) != identity:
            raise ValueError("审批身份必须是规范 UUID")
    if type(value.wait_version) is not int or value.wait_version < 1:
        raise ValueError("审批等待版本必须为正整数")
    if not isinstance(value.action_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", value.action_hash
    ):
        raise ValueError("审批动作哈希无效")
    if value.decision not in {"approved", "rejected"}:
        raise ValueError("审批决定只能为批准或拒绝")
    if not isinstance(value.actor, str) or not value.actor.strip() or len(value.actor) > 200:
        raise ValueError("审批必须记录操作人")


class ApprovalTicket(ToolModel):
    approval_id: UUID
    task_id: UUID
    wait_version: int = Field(ge=1)
    plan_evidence_id: UUID
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: ActionPlan
