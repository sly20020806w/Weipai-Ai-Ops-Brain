"""执行后的验证目标必须从真实回执继承，不能改写成更容易通过的目标。"""

import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.kubernetes.execution import ExecutionReceipt
from app.executor.models import ExecutionCommand
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.verifier.models import VerificationSpec


async def require_execution_target(session: AsyncSession, spec: VerificationSpec) -> None:
    ledger = LedgerService(session)
    records = await ledger.evidence_for_task(spec.task_id)
    checkpoints = []
    for record in records:
        phase = record.parameters.get("task")
        if record.source_tool == "execution.complete" and isinstance(phase, dict):
            if phase.get("version") == spec.verifying_version - 1:
                checkpoints.append(record)
    if not checkpoints:
        if any(e.source_tool in {"execute_action", "action_plan"} for e in records):
            raise ValueError("动作未完成，不能用其他版本的验证规格")
        return  # 保留 Step 31 独立只读入口和 Step 17 历史占位兼容。
    if len(checkpoints) != 1 or not isinstance(checkpoints[0].result_snapshot, dict):
        raise ValueError("缺少唯一执行完成检查点")
    ids = checkpoints[0].result_snapshot.get("evidence_ids")
    if not isinstance(ids, list) or not ids or not isinstance(ids[-1], str):
        raise ValueError("执行完成检查点缺少回执")
    evidence = await ledger.get_evidence(UUID(ids[-1]))
    command = ExecutionCommand.model_validate_json(json.dumps(evidence.parameters))
    receipt = ExecutionReceipt.model_validate_json(json.dumps(evidence.result_snapshot))
    target = receipt.target
    audits = await ledger.audits_for_task(spec.task_id)
    if (
        evidence.task_id != spec.task_id
        or evidence.source_tool != "execute_action"
        or not any(
            a.evidence_id == evidence.id
            and a.event_type is AuditEventType.EXECUTION
            and a.outcome == "succeeded"
            for a in audits
        )
    ):
        raise ValueError("验证依据不是当前任务的真实执行回执")
    if (
        spec.action_id != command.action_id
        or spec.action_completed_at != receipt.completed_at
        or spec.service_name != target.service_name
        or spec.cluster_name != target.cluster_name
        or spec.namespace != target.namespace
        or spec.deployment_name != target.deployment_name
        or spec.container_name != target.container_name
        or spec.expected_image != target.image
        or spec.expected_replicas != target.replicas
    ):
        raise ValueError("独立验证目标必须与最后一项成功动作的实际回执一致")
