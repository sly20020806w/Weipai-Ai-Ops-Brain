"""任务锁下留证和停止自动化；通知失败不回滚熔断，持久化锁存不自动复位。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.safety.engine import evaluate
from app.tasks.safety.models import (
    REASON_TEXT,
    AuditFact,
    AutomationAborted,
    EvidenceFact,
    SafetyConfig,
    SafetyResult,
    config_hash,
)
from app.tasks.service import TaskService, TaskStateConflict
from app.tasks.states import ALLOWED_TRANSITIONS, TaskStatus
from app.tasks.workflow_models import TaskSnapshot
from app.tools.registry import json_object


async def abort_record(session: AsyncSession, task_id: UUID) -> Evidence | None:
    records = await session.scalars(
        select(Evidence).where(Evidence.task_id == task_id, Evidence.source_tool == "safety.abort")
    )
    return records.one_or_none()


async def require_automation_active(session: AsyncSession, task_id: UUID) -> None:
    from app.tasks.takeover import takeover_record

    if await takeover_record(session, task_id) is not None:
        raise AutomationAborted("人工已接管，后续自动动作被禁止")
    if await abort_record(session, task_id) is not None:
        raise AutomationAborted("任务已自动熔断，必须由人工接管；旧审批不能恢复自动执行")


class SafetyService:
    def __init__(self, session: AsyncSession, config: SafetyConfig) -> None:
        self.session, self.config = session, SafetyConfig.model_validate(config)

    async def check(
        self, task: AITask, *, pending_action: tuple[str, str] | None = None
    ) -> SafetyResult:
        if not self.session.in_transaction():
            raise RuntimeError("熔断服务需要外层事务")
        # 公共入口也持有行锁，与 Executor 签发和 TaskService 迁移串行化。
        locked = await self.session.scalar(
            select(AITask)
            .where(AITask.id == task.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise TaskStateConflict("熔断任务不存在")
        task = locked
        ledger = LedgerService(self.session)
        cached = await abort_record(self.session, task.id)
        snapshot = TaskSnapshot(str(task.id), task.status, task.status_version)
        if cached is not None:
            assert isinstance(cached.result_snapshot, dict)
            reasons = cached.result_snapshot["reasons"]
            assert isinstance(reasons, list)
            return SafetyResult(snapshot, str(cached.id), tuple(str(r) for r in reasons))
        if TaskStatus.AUTOMATION_ABORTED not in ALLOWED_TRANSITIONS[task.status]:
            return SafetyResult(snapshot)
        evidence = [
            EvidenceFact(e.id, e.source_tool, e.parameters, e.result_snapshot, e.collected_at)
            for e in await ledger.evidence_for_task(task.id)
            if isinstance(e.result_snapshot, dict)
        ]
        audits = [
            AuditFact(a.id, a.operation, a.outcome, a.occurred_at, a.evidence_id, a.details)
            for a in await ledger.audits_for_task(task.id)
            if a.event_type is AuditEventType.TOOL_CALL
        ]
        findings = evaluate(evidence, audits, self.config, pending_action=pending_action)
        if not findings:
            return SafetyResult(snapshot)
        record = await ledger.append_evidence(
            task_id=task.id,
            source_tool="safety.abort",
            parameters={
                "phase_version": task.status_version,
                "pending_action": list(pending_action) if pending_action else None,
            },
            result_snapshot={
                "reasons": [f.reason.value for f in findings],
                "findings": [json_object(f.model_dump(mode="json")) for f in findings],
                "config_hash": config_hash(self.config),
            },
        )
        explanation = "、".join(REASON_TEXT[f.reason] for f in findings)
        updated = await TaskService(self.session).transition(
            task.id,
            TaskStatus.AUTOMATION_ABORTED,
            expected_status=snapshot.status,
            expected_version=snapshot.version,
            reason=f"自动熔断：{explanation}；证据 {record.id}",
        )
        await ledger.append_audit(
            task_id=task.id,
            event_type=AuditEventType.EXECUTION,
            actor="workflow",
            operation="safety.abort",
            outcome="aborted",
            evidence_id=record.id,
            details={"reasons": [f.reason.value for f in findings]},
        )
        return SafetyResult(
            TaskSnapshot(str(task.id), updated.status, updated.status_version),
            str(record.id),
            tuple(f.reason.value for f in findings),
        )


class SafetyStore:
    def __init__(self, database: Database, config: SafetyConfig) -> None:
        self.database, self.config = database, config

    async def check(self, snapshot: TaskSnapshot) -> SafetyResult:
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == UUID(snapshot.task_id)).with_for_update()
            )
            if task is None:
                raise TaskStateConflict("熔断任务不存在")
            if await abort_record(session, task.id) is None and (
                task.status,
                task.status_version,
            ) != (snapshot.status, snapshot.version):
                raise TaskStateConflict("熔断检查版本已变化")
            return await SafetyService(session, self.config).check(task)
