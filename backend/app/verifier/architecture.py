"""独立核对评审覆盖、输入、引用和真实查询；评审完成不代表方案获得上线授权。"""

import json
from dataclasses import asdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import RiskLevel
from app.runbooks.schemas import MatchingFacts, applicability
from app.tasks.architecture.engine import validate_citations
from app.tasks.architecture.models import (
    ArchitectureReport,
    ArchitectureVerification,
    ArchitectureVerifyRequest,
)
from app.tasks.architecture.service import ACTOR, submission
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor, VerificationRequired
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus, JsonObject, ToolModel
from app.tools.registry import ToolRegistry, json_object
from app.tools.runbooks import SearchRunbooksOutput
from app.verifier.authority import _verification_scope


async def validate_report(
    session: AsyncSession, task: AITask, report_id: UUID
) -> ArchitectureReport:
    ledger = LedgerService(session)
    record = await ledger.get_evidence(report_id)
    report = ArchitectureReport.model_validate_json(json.dumps(record.result_snapshot))
    proposal, value = await submission(session, task)
    if (
        record.task_id != task.id
        or record.source_tool != "architecture.report"
        or report.task_id != task.id
        or report.submission_hash != value.digest
        or report.service_name != value.service_name
        or report.sources.proposal != proposal.id
        or report.phase_version != task.status_version - 4
        or record.parameters
        != {"phase_version": report.phase_version, "submission_hash": value.digest}
    ):
        raise VerificationRequired("架构评审报告与当前任务、阶段或输入不匹配")
    source_names = {
        "runbooks": "search_runbooks",
        "context": "get_service_context",
        "standards": "search_knowledge",
        "incidents": "search_incidents",
    }
    audits = await ledger.audits_for_task(task.id)
    snapshots: dict[UUID, JsonObject] = {proposal.id: json_object(proposal.result_snapshot)}
    sources = report.sources.model_dump()
    for role, name in source_names.items():
        evidence = await ledger.get_evidence(sources[role])
        if (
            evidence.task_id != task.id
            or evidence.source_tool != name
            or evidence.collected_at > record.collected_at
            or not any(
                a.event_type is AuditEventType.TOOL_CALL
                and a.actor == ACTOR
                and a.operation == name
                and a.evidence_id == evidence.id
                and a.outcome == "succeeded"
                and a.details.get("mode") == "live"
                for a in audits
            )
        ):
            raise VerificationRequired("评审来源缺少同任务真实查询和成功审计")
        p = evidence.parameters
        if (
            (role == "runbooks" and p.get("query") != f"{value.service_name} 架构评审")
            or (role == "context" and p.get("service_name") != value.service_name)
            or (
                role == "standards"
                and (
                    p.get("kind") != "standard"
                    or p.get("query") != f"{value.service_name} {value.proposal}"[:20000]
                )
            )
            or (
                role == "incidents"
                and (
                    p.get("query") != value.service_name
                    or p.get("service_name") != value.service_name
                )
            )
        ):
            raise VerificationRequired("评审查询范围与方案不一致")
        snapshots[evidence.id] = json_object(evidence.result_snapshot)
    if len(snapshots) != 5:
        raise VerificationRequired("评审必须包含五份独立输入与查询快照")
    # Runbook 必须是首个成功查询，并保存适用/排除判定；不执行其生产处理步骤。
    records = await ledger.evidence_for_task(task.id)
    contexts = [
        e
        for e in records
        if e.source_tool == "architecture.context" and e.parameters == record.parameters
    ]
    if len(contexts) != 1 or not isinstance(contexts[0].result_snapshot, dict):
        raise VerificationRequired("缺少唯一的已提交评审上下文")
    if contexts[0].result_snapshot.get("sources") != report.sources.model_dump(mode="json"):
        raise VerificationRequired("评审报告引用与已提交查询检查点不一致")
    matching = [
        e
        for e in records
        if e.source_tool == "architecture.runbook_matching" and e.parameters == record.parameters
    ]
    if len(matching) != 1 or not isinstance(matching[0].result_snapshot, dict):
        raise VerificationRequired("缺少架构评审先行 Runbook 条件核对")
    if matching[0].result_snapshot.get("search_evidence_id") != str(report.sources.runbooks):
        raise VerificationRequired("Runbook 条件核对引用错误")
    runbook = await ledger.get_evidence(report.sources.runbooks)
    hits = SearchRunbooksOutput.model_validate_json(json.dumps(runbook.result_snapshot))
    decisions = [
        list(
            applicability(
                hit.runbook,
                MatchingFacts(
                    service_name=value.service_name, title=task.title, task_source=task.source.value
                ),
            )
        )
        for hit in hits.matches
    ]
    if matching[0].result_snapshot.get("decisions") != decisions:
        raise VerificationRequired("Runbook 适用/排除判定与原始快照不一致")
    for role in ("context", "standards", "incidents"):
        if (await ledger.get_evidence(sources[role])).collected_at < runbook.collected_at:
            raise VerificationRequired("Runbook 必须先于其他事实查询")
    validate_citations(report, snapshots)
    return report


async def validate_architecture_verification(
    session: AsyncSession, task: AITask, evidence: Evidence, *, require_passed: bool
) -> ArchitectureVerification:
    result = ArchitectureVerification.model_validate_json(json.dumps(evidence.result_snapshot))
    if (
        evidence.task_id != task.id
        or evidence.source_tool != "verify_architecture"
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
        raise VerificationRequired("独立架构评审验证身份或版本无效")
    if not any(
        a.event_type is AuditEventType.TOOL_CALL
        and a.actor == "verifier"
        and a.operation == "verify_architecture"
        and a.evidence_id == evidence.id
        and a.outcome == "succeeded"
        and a.details.get("mode") == "live"
        for a in await LedgerService(session).audits_for_task(task.id)
    ):
        raise VerificationRequired("缺少独立架构评审验证审计")
    await validate_report(session, task, result.report_evidence_id)
    return result


class VerificationQuery(ToolModel):
    task_id: UUID
    verifying_version: int
    report_evidence_id: UUID


class ArchitectureVerifier:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    @activity.defn(name="verifier.architecture")
    async def verify(self, request: ArchitectureVerifyRequest) -> TaskSnapshot:
        try:
            key = json_object(asdict(request))
            async with self.database.session() as session, session.begin():
                task = await session.scalar(
                    select(AITask)
                    .where(AITask.id == UUID(request.task.task_id))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if task is None or request.task.status is not TaskStatus.VERIFYING:
                    raise VerificationRequired("架构评审验证任务或阶段错误")
                ledger = LedgerService(session)
                cached = next(
                    (
                        e
                        for e in await ledger.evidence_for_task(task.id)
                        if e.source_tool == "architecture.verified" and e.parameters == key
                    ),
                    None,
                )
                if cached is not None:
                    history = await session.scalar(
                        select(TaskStatusHistory).where(
                            TaskStatusHistory.task_id == task.id,
                            TaskStatusHistory.sequence == request.task.version + 1,
                        )
                    )
                    if history is None or history.to_status is not TaskStatus.RESOLVED:
                        raise VerificationRequired("验证检查点缺少实际状态迁移")
                    return TaskSnapshot(str(task.id), TaskStatus.RESOLVED, request.task.version + 1)
                if (
                    task.status is not request.task.status
                    or task.status_version != request.task.version
                ):
                    raise VerificationRequired("架构评审验证版本错误")
                registry = ToolRegistry()

                async def verify(query: VerificationQuery) -> ArchitectureVerification:
                    if query.task_id != task.id or query.verifying_version != task.status_version:
                        raise VerificationRequired("独立评审验证范围错误")
                    await validate_report(session, task, query.report_evidence_id)
                    return ArchitectureVerification(**query.model_dump(), passed=True)

                registry.register(
                    name="verify_architecture",
                    description="独立核对十二维架构评审证据",
                    input_model=VerificationQuery,
                    output_model=ArchitectureVerification,
                    handler=verify,
                    risk_level=RiskLevel.L0,
                )
                query = VerificationQuery(
                    task_id=task.id,
                    verifying_version=task.status_version,
                    report_evidence_id=UUID(request.report_evidence_id),
                )
                parameters = query.model_dump(mode="json")
                # passed 是内部固定值，不能通过模型/调用方参数设置。
                dispatch = await ToolDispatcher(
                    registry, create_policy_engine(self.settings), ledger
                ).dispatch(
                    task_id=task.id,
                    tool_name="verify_architecture",
                    parameters=parameters,
                    actor="verifier",
                )
                if dispatch.status is DispatchStatus.SUCCEEDED and dispatch.evidence_id is not None:
                    with _verification_scope(task.id, task.status_version, dispatch.evidence_id):
                        changed = await TaskService(session).transition(
                            task.id,
                            TaskStatus.RESOLVED,
                            expected_status=task.status,
                            expected_version=task.status_version,
                            actor=TransitionActor.VERIFIER,
                            reason="十二维评审及证据核验完成；风险与待补充项保留，未批准任何变更",
                        )
                    await ledger.append_evidence(
                        task_id=task.id,
                        source_tool="architecture.verified",
                        parameters=key,
                        result_snapshot={"evidence_id": str(dispatch.evidence_id)},
                    )
                    return TaskSnapshot(str(changed.id), changed.status, changed.status_version)
            # 拒绝和失败也必须提交调用审计；只能在离开事务后报告失败。
            raise VerificationRequired("独立架构评审验证未通过")
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("独立架构评审验证被拒绝", non_retryable=True) from None
