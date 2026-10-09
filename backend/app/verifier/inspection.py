"""独立验证巡检报告与风险落库，不把巡检完成解释为风险已修复。"""

import json
from contextvars import ContextVar
from dataclasses import asdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.inspection.models import InspectionFacts
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import RiskLevel
from app.tasks.inspection.engine import evaluate
from app.tasks.inspection.models import (
    InspectionConfig,
    InspectionReport,
    InspectionVerification,
    InspectionVerifyRequest,
    RiskEntry,
    risk_key,
)
from app.tasks.inspection.service import cached
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor, VerificationRequired
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus, ToolModel
from app.tools.registry import ToolRegistry, json_object
from app.verifier.authority import _verification_scope

_config: ContextVar[InspectionConfig | None] = ContextVar(
    "inspection_verification_config", default=None
)


async def validate_report(
    session: AsyncSession,
    task: AITask,
    evidence_id: UUID,
    settings: Settings | None = None,
) -> InspectionReport:
    ledger = LedgerService(session)
    record = await ledger.get_evidence(evidence_id)
    report = InspectionReport.model_validate_json(json.dumps(record.result_snapshot))
    if (
        record.task_id != task.id
        or record.source_tool != "inspection.report"
        or report.task_id != task.id
        or record.parameters
        != {
            "task": {
                "task_id": str(task.id),
                "status": "INVESTIGATING",
                "version": report.phase_version,
            },
            "mode": report.mode,
        }
    ):
        raise VerificationRequired("巡检报告身份或来源不匹配")
    if not report.complete or report.phase_version != task.status_version - 4:
        raise VerificationRequired("巡检报告不完整或阶段版本错误")
    config = settings.inspection_config if settings is not None else _config.get()
    if config is None:
        raise VerificationRequired("独立巡检配置上下文缺失")
    if config.fingerprint != report.rule_fingerprint:
        raise VerificationRequired("巡检规则已变化，须重新扫描")
    audits = await ledger.audits_for_task(task.id)
    if {c.service_name for c in report.checks} != set(config.services):
        raise VerificationRequired("巡检服务覆盖范围不完整")
    for service in config.services:
        checks = tuple(c for c in report.checks if c.service_name == service)
        ids = {c.evidence_id for c in checks}
        if len(ids) != 1 or None in ids:
            raise VerificationRequired("巡检结论必须引用同服务真实事实")
        fact_id = checks[0].evidence_id
        assert fact_id is not None
        evidence = await ledger.get_evidence(fact_id)
        facts = InspectionFacts.model_validate_json(json.dumps(evidence.result_snapshot))
        if (
            evidence.task_id != task.id
            or evidence.source_tool != "query_inspection_facts"
            or evidence.parameters != {"service_name": service}
            or not any(
                a.actor == "inspection"
                and a.operation == evidence.source_tool
                and a.outcome == "succeeded"
                and a.evidence_id == fact_id
                and a.details.get("mode") == "live"
                for a in audits
            )
            or checks
            != evaluate(service, report.mode, config, facts, fact_id, checks[0].assessed_at)
        ):
            raise VerificationRequired("巡检结论与真实查询及审计不一致")
        for check in checks:
            if check.outcome == "abnormal":
                risk = await session.scalar(
                    select(RiskEntry).where(RiskEntry.risk_key == risk_key(check))
                )
                if risk is None:
                    raise VerificationRequired("异常尚未保存为风险")
                changes = [
                    e
                    for e in await ledger.evidence_for_task(task.id)
                    if e.source_tool == "inspection.risk"
                    and e.result_snapshot == check.model_dump(mode="json")
                ]
                if not changes:
                    raise VerificationRequired("风险缺少与本次结论相同的留证")
    if len(report.runbook_evidence_ids) != len(config.services):
        raise VerificationRequired("巡检缺少先行 Runbook 检索")
    for index, service in enumerate(sorted(config.services)):
        evidence = await ledger.get_evidence(report.runbook_evidence_ids[index])
        if (
            evidence.task_id != task.id
            or evidence.source_tool != "search_runbooks"
            or evidence.parameters.get("query") != f"{service} 巡检 治理"
            or not any(
                a.evidence_id == evidence.id
                and a.actor == "inspection"
                and a.outcome == "succeeded"
                and a.details.get("mode") == "live"
                for a in audits
            )
        ):
            raise VerificationRequired("Runbook 检索缺少有效查询和审计")
    return report


async def validate_inspection_verification(
    session: AsyncSession,
    task: AITask,
    evidence: Evidence,
    *,
    require_passed: bool,
) -> InspectionVerification:
    result = InspectionVerification.model_validate_json(json.dumps(evidence.result_snapshot))
    if (
        evidence.task_id != task.id
        or evidence.source_tool != "verify_inspection"
        or result.task_id != task.id
        or result.verifying_version != task.status_version
        or (require_passed and not result.passed)
        or evidence.parameters
        != {
            "task_id": str(task.id),
            "verifying_version": task.status_version,
            "report_evidence_id": str(result.report_evidence_id),
        }
    ):
        raise VerificationRequired("独立巡检验证版本或证据无效")
    audits = await LedgerService(session).audits_for_task(task.id)
    if not any(
        a.event_type is AuditEventType.TOOL_CALL
        and a.actor == "verifier"
        and a.operation == "verify_inspection"
        and a.outcome == "succeeded"
        and a.evidence_id == evidence.id
        and a.details.get("mode") == "live"
        for a in audits
    ):
        raise VerificationRequired("缺少独立巡检验证审计")
    await validate_report(session, task, result.report_evidence_id)
    return result


class VerificationQuery(ToolModel):
    task_id: UUID
    verifying_version: int
    report_evidence_id: UUID


class InspectionVerifier:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    @activity.defn(name="verifier.inspection")
    async def verify(self, request: InspectionVerifyRequest) -> TaskSnapshot:
        try:
            key = json_object(asdict(request))
            async with self.database.session() as session, session.begin():
                saved = await cached(
                    session, UUID(request.task.task_id), "inspection.verified", key
                )
                if saved is not None:
                    return TaskSnapshot(
                        request.task.task_id, TaskStatus.RESOLVED, request.task.version + 1
                    )
                task = await session.scalar(
                    select(AITask)
                    .where(AITask.id == UUID(request.task.task_id))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if task is None:
                    raise ValueError("巡检验证任务不存在")
                saved = await cached(session, task.id, "inspection.verified", key)
                if saved is not None:
                    return TaskSnapshot(
                        request.task.task_id, TaskStatus.RESOLVED, request.task.version + 1
                    )
                if (
                    task.status is not TaskStatus.VERIFYING
                    or task.status_version != request.task.version
                ):
                    raise ValueError("巡检只能在 VERIFYING 独立验证")
                ledger = LedgerService(session)
                registry = ToolRegistry()

                async def verify(query: VerificationQuery) -> InspectionVerification:
                    if query.task_id != task.id or query.verifying_version != task.status_version:
                        raise ValueError("验证任务或版本不匹配")
                    await validate_report(session, task, query.report_evidence_id, self.settings)
                    return InspectionVerification(**query.model_dump(), passed=True)

                registry.register(
                    name="verify_inspection",
                    description="独立核对巡检报告和风险落库",
                    input_model=VerificationQuery,
                    output_model=InspectionVerification,
                    handler=verify,
                    risk_level=RiskLevel.L0,
                )
                result = await ToolDispatcher(
                    registry, create_policy_engine(self.settings), ledger
                ).dispatch(
                    task_id=task.id,
                    tool_name="verify_inspection",
                    actor="verifier",
                    parameters=VerificationQuery(
                        task_id=task.id,
                        verifying_version=task.status_version,
                        report_evidence_id=UUID(request.report_evidence_id),
                    ).model_dump(mode="json"),
                )
                if result.status is not DispatchStatus.SUCCEEDED or result.evidence_id is None:
                    raise VerificationRequired("独立巡检验证失败")
                token = _config.set(self.settings.inspection_config)
                try:
                    with _verification_scope(task.id, task.status_version, result.evidence_id):
                        changed = await TaskService(session).transition(
                            task.id,
                            TaskStatus.RESOLVED,
                            expected_status=task.status,
                            expected_version=task.status_version,
                            actor=TransitionActor.VERIFIER,
                            reason="巡检报告与风险独立核验完成；风险状态仍独立保留",
                        )
                finally:
                    _config.reset(token)
                await ledger.append_evidence(
                    task_id=task.id,
                    source_tool="inspection.verified",
                    parameters=key,
                    result_snapshot={"evidence_id": str(result.evidence_id)},
                )
                return TaskSnapshot(str(changed.id), changed.status, changed.status_version)
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("独立巡检验证被拒绝", non_retryable=True) from None
