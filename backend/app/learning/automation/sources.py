"""只统计本库事实引用；回放、改进任务和重投不放大劳动次数。"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.automation.models import ManualOperation, ScanWindow, WorkKind, WorkRecord
from app.learning.models import IncidentReport
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.runbooks.schemas import RunbookView
from app.tasks.states import TaskSource
from app.triggers.models import OpsEvent


def event_record(event: OpsEvent) -> WorkRecord | None:
    if event.origin == "learning":
        return None
    if event.source == TaskSource.TICKET.value:
        kind, signature = WorkKind.TICKET, event.title
    elif (
        event.source == TaskSource.SCHEDULE.value
        and event.origin == "schedule"
        and event.external_id.startswith("release-verification:")
    ):
        kind, signature = WorkKind.RELEASE_CHECK, "release-verification"
    else:
        return None
    return WorkRecord(
        kind=kind,
        service_name=event.service_name,
        signature=signature,
        table="ops_events",
        record_id=event.id,
        task_id=event.task_id,
        occurred_at=event.occurred_at,
    )


def evidence_record(
    evidence: Evidence, event: OpsEvent | None, accepted: set[UUID]
) -> WorkRecord | None:
    if event is not None and event.origin == "learning":
        return None
    snapshot = evidence.result_snapshot
    if not isinstance(snapshot, dict):
        return None
    if evidence.source_tool == "postmortem":
        report = IncidentReport.model_validate_json(json.dumps(snapshot))
        if report.task_id != evidence.task_id:
            raise ValueError("复盘与来源任务不一致")
        root = report.sections[4]
        if any(
            word in claim.statement
            for claim in root.conclusions
            for word in ("待核实", "未确认", "未知")
        ):
            return None
        kind, service = WorkKind.INCIDENT, report.service_name
        signature = "；".join(sorted(claim.statement for claim in root.conclusions))
    elif evidence.source_tool == "runbook.match":
        if event is None or snapshot.get("blocked") is not False:
            return None
        payload = snapshot.get("runbook_json")
        if not isinstance(payload, str) or snapshot.get("search_evidence_id") is None:
            return None
        if UUID(str(snapshot["search_evidence_id"])) not in accepted:
            return None
        runbook = RunbookView.model_validate_json(payload)
        kind, service = WorkKind.RUNBOOK, event.service_name
        signature = f"{runbook.id}:v{runbook.content_version}"
    else:
        return None
    return WorkRecord(
        kind=kind,
        service_name=service,
        signature=signature,
        table="evidence_ledger",
        record_id=evidence.id,
        task_id=evidence.task_id,
        occurred_at=evidence.collected_at,
        evidence_id=evidence.id,
    )


def manual_record(
    audit: AuditRecord, evidence: Evidence, event: OpsEvent | None
) -> WorkRecord | None:
    if (
        audit.event_type is not AuditEventType.HUMAN_INTERACTION
        or audit.outcome != "recorded"
        or audit.details.get("mode") == "replay"
        or audit.actor.startswith("replay:")
        or audit.task_id != evidence.task_id
        or audit.evidence_id != evidence.id
        or audit.operation != evidence.source_tool
        or (event is not None and event.origin == "learning")
    ):
        return None
    snapshot = evidence.result_snapshot
    if not isinstance(snapshot, dict):
        return None
    if audit.operation == "manual.operation":
        operation = ManualOperation.model_validate_json(json.dumps(snapshot))
        if operation.task_id != audit.task_id or operation.actor != audit.actor:
            raise ValueError("人工操作证据与操作人审计不一致")
        service, signature = operation.service_name, f"operation:{operation.operation}"
    elif audit.operation == "human.answer" and event is not None:
        question = snapshot.get("question")
        if not isinstance(question, str) or not question.strip():
            return None
        if snapshot.get("respondent") != audit.actor:
            raise ValueError("人工回答与操作人审计不一致")
        service, signature = event.service_name, f"question:{question}"
    else:
        return None
    return WorkRecord(
        kind=WorkKind.MANUAL,
        service_name=service,
        signature=signature,
        table="audit_log",
        record_id=audit.id,
        task_id=audit.task_id,
        occurred_at=audit.occurred_at,
        evidence_id=evidence.id,
    )


async def collect_records(session: AsyncSession, window: ScanWindow) -> tuple[WorkRecord, ...]:
    window = ScanWindow.model_validate(window)
    # 上下文允许早于统计窗口，但迟到的关联/证据不能进入本轮截止点。
    events = list(await session.scalars(select(OpsEvent).where(OpsEvent.created_at < window.end)))
    by_task = {event.task_id: event for event in events}
    result = []
    for event in events:
        if window.start <= event.occurred_at < window.end:
            record = event_record(event)
            if record is not None:
                result.append(record)
    evidence = list(
        await session.scalars(
            select(Evidence).where(
                Evidence.collected_at < window.end,
                Evidence.created_at < window.end,
                Evidence.source_tool.in_(
                    (
                        "postmortem",
                        "runbook.match",
                        "manual.operation",
                        "human.answer",
                        "search_runbooks",
                    )
                ),
            )
        )
    )
    audits = list(
        await session.scalars(
            select(AuditRecord).where(
                AuditRecord.occurred_at < window.end,
                AuditRecord.created_at < window.end,
                AuditRecord.operation.in_(("search_runbooks", "manual.operation", "human.answer")),
            )
        )
    )
    by_id = {item.id: item for item in evidence}
    accepted_by_task: dict[UUID, set[UUID]] = {}
    for audit in audits:
        supporting = by_id.get(audit.evidence_id) if audit.evidence_id else None
        if (
            audit.evidence_id is not None
            and supporting is not None
            and supporting.source_tool == "search_runbooks"
            and supporting.task_id == audit.task_id
            and audit.event_type is AuditEventType.TOOL_CALL
            and audit.operation == "search_runbooks"
            and audit.outcome == "succeeded"
            and audit.details.get("mode") == "live"
            and not audit.actor.startswith("replay:")
        ):
            accepted_by_task.setdefault(audit.task_id, set()).add(audit.evidence_id)
    # 同一任务多个 RCA/Runbook 阶段只计一次相同劳动，保留第一条真实记录。
    seen: set[tuple[UUID, str]] = set()
    for item in sorted(evidence, key=lambda e: (e.collected_at, str(e.id))):
        if not window.start <= item.collected_at < window.end:
            continue
        record = evidence_record(
            item, by_task.get(item.task_id), accepted_by_task.get(item.task_id, set())
        )
        if record is not None and (record.task_id, record.group_key) not in seen:
            result.append(record)
            seen.add((record.task_id, record.group_key))
    seen_manual: set[UUID] = set()
    for audit in sorted(audits, key=lambda a: (a.occurred_at, str(a.id))):
        manual_evidence = by_id.get(audit.evidence_id) if audit.evidence_id else None
        if manual_evidence is None or not window.start <= audit.occurred_at < window.end:
            continue
        record = manual_record(audit, manual_evidence, by_task.get(audit.task_id))
        if record is not None and manual_evidence.id not in seen_manual:
            result.append(record)
            seen_manual.add(manual_evidence.id)
    return tuple(result)
