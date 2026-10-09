"""仅独立验证服务可持有的任务/版本/证据绑定权限；actor 字符串不是授权。"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.states import VerificationRequired
from app.verifier.models import VerificationReport

_permit: ContextVar[tuple[UUID, int, UUID] | None] = ContextVar("verification_permit", default=None)


class VerificationOutcome(Protocol):
    @property
    def passed(self) -> bool: ...


@contextmanager
def _verification_scope(task_id: UUID, version: int, evidence_id: UUID) -> Iterator[None]:
    token = _permit.set((task_id, version, evidence_id))
    try:
        yield
    finally:
        _permit.reset(token)


async def require_verification(session: AsyncSession, task: AITask) -> None:
    permit = _permit.get()
    if permit is None or permit[:2] != (task.id, task.status_version):
        raise VerificationRequired("RESOLVED 必须由独立 Verifier 持当前版本验证证据设置")
    await validate_verification_facts(session, task, permit[2], require_passed=True)


async def require_verification_outcome(
    session: AsyncSession,
    task: AITask,
    evidence_id: UUID,
) -> VerificationOutcome | None:
    if _permit.get() != (task.id, task.status_version, evidence_id):
        raise VerificationRequired("Runbook 计数只能由独立 Verifier 持当前证据更新")
    return await validate_verification_facts(session, task, evidence_id, require_passed=False)


async def validate_verification_facts(
    session: AsyncSession,
    task: AITask,
    evidence_id: UUID,
    *,
    require_passed: bool,
) -> VerificationOutcome | None:
    ledger = LedgerService(session)
    evidence = await ledger.get_evidence(evidence_id)
    if evidence.source_tool == "verify_chat":
        from app.verifier.chat import validate_chat_verification

        return await validate_chat_verification(
            session, task, evidence, require_passed=require_passed
        )
    if evidence.source_tool == "verify_war_room":
        from app.verifier.war_room import validate_war_room_verification

        return await validate_war_room_verification(
            session, task, evidence, require_passed=require_passed
        )
    if evidence.source_tool == "verify_architecture":
        from app.verifier.architecture import validate_architecture_verification

        return await validate_architecture_verification(
            session, task, evidence, require_passed=require_passed
        )
    if evidence.source_tool == "verify_inspection":
        from app.verifier.inspection import validate_inspection_verification

        return await validate_inspection_verification(
            session, task, evidence, require_passed=require_passed
        )
    if evidence.source_tool == "verify_release":
        from app.verifier.releases import validate_release_report

        return await validate_release_report(session, task, evidence, require_passed=require_passed)
    if evidence.source_tool in {"verify_ticket", "verify_ticket_permission"}:
        from app.verifier.tickets import validate_ticket_report

        if require_passed and evidence.source_tool != "verify_ticket":
            raise VerificationRequired("仅权限验证不能设置 RESOLVED，还必须验证工单回填关闭")
        return await validate_ticket_report(
            session,
            task,
            evidence,
            final=evidence.source_tool == "verify_ticket",
            require_passed=require_passed,
        )
    report = VerificationReport.model_validate_json(json.dumps(evidence.result_snapshot))
    if (
        evidence.task_id != task.id
        or evidence.source_tool != "verify_action"
        or report.spec.task_id != task.id
        or report.spec.verifying_version != task.status_version
        or (require_passed and not report.passed)
        or evidence.parameters != report.spec.model_dump(mode="json")
    ):
        raise VerificationRequired("恢复证据不属于当前任务和版本，或验证未通过")
    audits = await ledger.audits_for_task(task.id)
    if not any(
        a.event_type is AuditEventType.TOOL_CALL
        and a.operation == "verify_action"
        and a.actor == "verifier"
        and a.outcome == "succeeded"
        and a.evidence_id == evidence.id
        and a.details.get("mode") == "live"
        for a in audits
    ):
        raise VerificationRequired("缺少独立 Verifier 的成功调用审计")
    for check in report.checks:
        if check.evidence_id is None:
            return None
        fact = await ledger.get_evidence(check.evidence_id)
        expected_tool = {
            "deployment": "get_k8s_status",
            "pods": "get_service_runtime",
            "http_5xx_ratio": "query_metrics",
            "http_p99_ms": "query_metrics",
            "http_success_ratio": "query_metrics",
            "logs": "query_logs",
            "traces": "query_traces",
            "resources": "get_cloud_resources",
        }[check.name]
        parameters = fact.parameters
        if (
            fact.source_tool != expected_tool
            or parameters.get("service_name") != report.spec.service_name
            or fact.collected_at > evidence.collected_at
        ):
            raise VerificationRequired("验证事实 Tool、服务或采集时间不匹配")
        if check.name in {"deployment", "pods"}:
            if parameters.get("namespace") != report.spec.namespace:
                raise VerificationRequired("验证 Kubernetes 事实的命名空间不匹配")
        elif parameters.get("start") != report.spec.start.isoformat().replace(
            "+00:00", "Z"
        ) or parameters.get("end") != report.spec.end.isoformat().replace("+00:00", "Z"):
            raise VerificationRequired("验证事实窗口与报告不匹配")
        if check.name.startswith("http_") and parameters.get("metric_name") != check.name:
            raise VerificationRequired("验证指标事实与检查项不匹配")
        if fact.task_id != task.id or not any(
            a.event_type is AuditEventType.TOOL_CALL
            and a.actor == "verifier"
            and a.outcome == "succeeded"
            and a.evidence_id == fact.id
            and a.details.get("mode") == "live"
            for a in audits
        ):
            raise VerificationRequired("恢复报告必须引用同任务的独立事实查询证据")
    return report
