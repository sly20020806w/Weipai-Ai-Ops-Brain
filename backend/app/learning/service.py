"""学习阶段事务：复盘、改进 OpsEvent/Task、Draft Runbook 原子保存并幂等复用。"""

import json
from uuid import UUID

from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.investigation import AgentConclusion
from app.config import Settings
from app.db.session import Database
from app.learning.engine import compose
from app.learning.models import (
    IncidentHit,
    IncidentReport,
    IncidentSearch,
    LearningRequest,
    LearningResult,
    TimelineItem,
)
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.runbooks.embedding import embedding_client
from app.runbooks.service import RunbookService
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.planning.models import ActionPlan
from app.tasks.service import TaskStateConflict
from app.tasks.states import TaskSource, TaskStatus
from app.tools.registry import json_object
from app.tools.timeline import RecentChangesOutput
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventReceipt, NormalizedEvent
from app.triggers.service import EventService
from app.verifier.models import VerificationReport


class IncidentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(self, query: IncidentSearch) -> tuple[IncidentHit, ...]:
        query = IncidentSearch.model_validate(query)
        pattern = query.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = select(Evidence).where(
            Evidence.source_tool == "postmortem",
            cast(Evidence.result_snapshot, Text).ilike(f"%{pattern}%", escape="\\"),
        )
        if query.service_name is not None:
            statement = statement.where(
                Evidence.result_snapshot["service_name"].as_string() == query.service_name
            )
        records = await self.session.scalars(
            statement.order_by(Evidence.collected_at.desc(), Evidence.id).limit(query.limit)
        )
        return tuple(
            IncidentHit(
                evidence_id=e.id,
                report=IncidentReport.model_validate_json(json.dumps(e.result_snapshot)),
            )
            for e in records
        )


class LearningStore:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    async def generate(self, request: LearningRequest) -> LearningResult:
        expected = request.task
        if expected.status is not TaskStatus.LEARNING:
            raise TaskStateConflict("只能对 LEARNING 阶段的已结束事故生成复盘")
        task_id = UUID(expected.task_id)
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == task_id).with_for_update()
            )
            if task is None:
                raise TaskStateConflict("复盘任务不存在")
            ledger = LedgerService(session)
            records = await ledger.evidence_for_task(task_id)
            cached = next(
                (
                    e
                    for e in records
                    if e.source_tool == "postmortem"
                    and e.parameters.get("phase_version") == expected.version
                ),
                None,
            )
            if cached:
                report = IncidentReport.model_validate_json(json.dumps(cached.result_snapshot))
                cached_events = await session.scalars(
                    select(OpsEvent).where(OpsEvent.task_id.in_(report.improvement_task_ids))
                )
                receipts = [
                    EventReceipt(str(e.id), str(e.task_id), f"ai-task-{e.task_id}", False)
                    for e in cached_events
                ]
                return LearningResult(
                    str(cached.id),
                    report.model_dump_json(),
                    sorted(receipts, key=lambda r: r.task_id),
                )
            if (task.status, task.status_version) != (expected.status, expected.version):
                raise TaskStateConflict("复盘任务状态或版本已变化")
            history_rows = await session.scalars(
                select(TaskStatusHistory)
                .where(TaskStatusHistory.task_id == task_id)
                .order_by(TaskStatusHistory.sequence)
            )
            history = list(history_rows)
            if not history or history[-1].from_status not in {
                TaskStatus.RESOLVED,
                TaskStatus.FAILED,
                TaskStatus.ESCALATED,
            }:
                raise TaskStateConflict("事故尚未结束，不能进入学习")
            event = await session.scalar(select(OpsEvent).where(OpsEvent.task_id == task_id))
            if event is not None and event.origin == "learning":
                raise TaskStateConflict("改进任务不是新事故，不递归生成改进任务")
            audits = await ledger.audits_for_task(task_id)
            # 不接受 think/observe 检查点或未执行的计划作为事实。
            successful = {
                a.evidence_id
                for a in audits
                if a.outcome == "succeeded"
                and a.event_type in {AuditEventType.TOOL_CALL, AuditEventType.EXECUTION}
            }
            accepted = [
                e
                for e in records
                if e.id in successful
                or e.source_tool
                in {
                    "agent.conclusion",
                    "reviewer.verdict",
                    "action_plan",
                }
            ]
            accepted = [
                e
                for e in accepted
                if e.source_tool != "verify_action"
                or any(
                    a.evidence_id == e.id
                    and a.actor == "verifier"
                    and a.operation == "verify_action"
                    and a.event_type is AuditEventType.TOOL_CALL
                    and a.outcome == "succeeded"
                    for a in audits
                )
            ]
            verification = next(
                (e for e in reversed(accepted) if e.source_tool == "verify_action"), None
            )
            if history[-1].from_status is TaskStatus.RESOLVED:
                if verification is None:
                    raise ValueError("已恢复事故必须有独立 Verifier 证据")
                verified = VerificationReport.model_validate_json(
                    json.dumps(verification.result_snapshot)
                )
                if (
                    not verified.passed
                    or verified.spec.task_id != task_id
                    or verified.spec.verifying_version != expected.version - 2
                ):
                    raise ValueError("复盘验证证据不属于本次独立恢复检查")
            conclusion = next(
                (e for e in reversed(accepted) if e.source_tool == "agent.conclusion"), None
            )
            if conclusion is not None:
                rca_versions = {h.sequence for h in history if h.to_status is TaskStatus.RCA}
                parsed = AgentConclusion.model_validate_json(json.dumps(conclusion.result_snapshot))
                observed = {
                    a.evidence_id
                    for a in audits
                    if a.actor == "codex-main-agent"
                    and a.event_type is AuditEventType.TOOL_CALL
                    and a.outcome == "succeeded"
                }
                if (
                    conclusion.parameters.get("phase_version") not in rca_versions
                    or not parsed.evidence_ids <= observed
                ):
                    raise ValueError("复盘根因不是本事故已验证的 RCA 结论")
            for entry in accepted:
                if entry.source_tool == "action_plan":
                    plan = ActionPlan.model_validate_json(json.dumps(entry.result_snapshot))
                    if (
                        plan.task_id != task_id
                        or conclusion is None
                        or plan.conclusion_evidence_id != conclusion.id
                    ):
                        raise ValueError("复盘处置计划与本事故 RCA 不一致")
            service_name = (
                event.service_name
                if event
                else (
                    str(verification.parameters["service_name"])
                    if verification
                    else "unassociated-service"
                )
            )
            context = await ledger.append_evidence(
                task_id=task_id,
                source_tool="postmortem.context",
                parameters={"phase_version": expected.version},
                result_snapshot=json_object(
                    {
                        "title": task.title,
                        "service_name": service_name,
                        "event": {
                            "id": str(event.id),
                            "origin": event.origin,
                            "occurred_at": event.occurred_at.isoformat(),
                        }
                        if event
                        else None,
                        "history": [
                            {
                                "status": h.to_status.value,
                                "time": h.changed_at.isoformat(),
                                "reason": h.reason,
                                "sequence": h.sequence,
                            }
                            for h in history
                        ],
                        "evidence_ids": [str(e.id) for e in accepted],
                    }
                ),
            )
            draft, runbook = compose(context.id, task.title, service_name, accepted)
            allowed = {context.id, *(e.id for e in accepted)}
            if not draft.evidence_ids <= allowed:
                raise ValueError("复盘引用并非本任务已接受的证据")
            saved_runbook = await RunbookService(
                session, lambda r: embedding_client(self.settings, r)
            ).create(runbook)
            events = [
                NormalizedEvent(
                    origin="learning",
                    source=TaskSource.AI,
                    external_id=f"postmortem:{task_id}:{expected.version}:{index}",
                    service_name=service_name,
                    title=f"事故改进：{claim.statement}"[:500],
                    occurred_at=context.collected_at,
                )
                for index, claim in enumerate(draft.improvements)
            ]
            receipts = await EventService(session).accept(events)
            for child in receipts:
                child_event = await session.get(OpsEvent, UUID(child.event_id))
                assert child_event is not None
                index = int(child_event.external_id.rsplit(":", 1)[1])
                await ledger.append_evidence(
                    task_id=UUID(child.task_id),
                    source_tool="postmortem.origin",
                    parameters={
                        "incident_task_id": str(task_id),
                        "context_evidence_id": str(context.id),
                    },
                    result_snapshot=json_object(draft.improvements[index].model_dump(mode="json")),
                )
            timeline = [
                TimelineItem(
                    occurred_at=h.changed_at,
                    description=f"{h.to_status.value}：{h.reason}",
                    evidence_id=context.id,
                )
                for h in history
            ]
            if event:
                timeline.append(
                    TimelineItem(
                        occurred_at=event.occurred_at,
                        description=f"事件：{event.title}",
                        evidence_id=context.id,
                    )
                )
            timeline.extend(
                TimelineItem(
                    occurred_at=e.collected_at,
                    description=f"证据采集：{e.source_tool}",
                    evidence_id=e.id,
                )
                for e in accepted
            )
            for entry in accepted:
                if entry.source_tool == "get_recent_changes":
                    changes = RecentChangesOutput.model_validate_json(
                        json.dumps(entry.result_snapshot)
                    )
                    timeline.extend(
                        TimelineItem(
                            occurred_at=change.occurred_at,
                            description=f"{change.kind}：{change.source_ref}",
                            evidence_id=entry.id,
                        )
                        for change in changes.events
                    )
            report = IncidentReport(
                **draft.model_dump(),
                task_id=task_id,
                service_name=service_name,
                title=task.title,
                timeline=tuple(sorted(timeline, key=lambda item: item.occurred_at)),
                runbook_id=saved_runbook.id,
                improvement_task_ids=tuple(sorted(UUID(r.task_id) for r in receipts)),
            )
            saved = await ledger.append_evidence(
                task_id=task_id,
                source_tool="postmortem",
                parameters={"phase_version": expected.version},
                result_snapshot=json_object(report.model_dump(mode="json")),
            )
            return LearningResult(
                str(saved.id), report.model_dump_json(), sorted(receipts, key=lambda r: r.task_id)
            )
