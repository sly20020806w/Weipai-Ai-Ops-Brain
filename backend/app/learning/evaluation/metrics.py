"""十项指标：显式分子/分母；未知标签及零样本不伪造为正确或 0%。"""

import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.investigation import AgentConclusion
from app.learning.evaluation.models import EvaluationLabel, root_cause_hit
from app.learning.evaluation.replay import available
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.states import TaskSource, TaskStatus
from app.tools.models import ToolModel
from app.tools.registry import json_object
from app.triggers.models import OpsEvent
from app.verifier.models import VerificationReport


class MetricSample(ToolModel):
    task_id: UUID
    rca_correct: bool | None = None
    runbook_attempted: bool = False
    runbook_hit: bool = False
    execution_attempted: bool = False
    execution_verified: bool = False
    approval_decisions: int = Field(default=0, ge=0)
    approval_rejections: int = Field(default=0, ge=0)
    human_takeover: bool = False
    alert: bool = False
    false_alert: bool | None = None
    mttr_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tool_calls: int = Field(default=0, ge=0)
    verification_attempts: int = Field(default=0, ge=0)
    verification_failures: int = Field(default=0, ge=0)
    automated: bool = False
    evidence_ids: tuple[UUID, ...] = ()
    history_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> "MetricSample":
        if (
            self.approval_rejections > self.approval_decisions
            or self.verification_failures > self.verification_attempts
            or (self.runbook_hit and not self.runbook_attempted)
            or (self.execution_verified and not self.execution_attempted)
            or (self.false_alert is not None and not self.alert)
        ):
            raise ValueError("指标分子不能超过分母且必须来自相应场景")
        return self


class MetricValue(ToolModel):
    name: str
    label: str
    numerator: float
    denominator: int
    value: float | None
    unit: str


class MetricsReport(ToolModel):
    samples: tuple[MetricSample, ...]
    metrics: Annotated[tuple[MetricValue, ...], Field(min_length=10, max_length=10)]


class EvaluationWindow(ToolModel):
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self) -> "EvaluationWindow":
        if self.start >= self.end:
            raise ValueError("指标窗口必须为 UTC 半开区间")
        return self


def calculate(samples: tuple[MetricSample, ...]) -> MetricsReport:
    samples = tuple(MetricSample.model_validate(s) for s in samples)
    if len({s.task_id for s in samples}) != len(samples):
        raise ValueError("同一任务不能重复进入指标样本")
    rca = [s for s in samples if s.rca_correct is not None]
    runbooks = [s for s in samples if s.runbook_attempted]
    executions = [s for s in samples if s.execution_attempted]
    alerts = [s for s in samples if s.alert and s.false_alert is not None]
    mttr = [s.mttr_seconds for s in samples if s.mttr_seconds is not None]
    entries: tuple[tuple[str, str, float, int, str], ...] = (
        ("rca_hit_rate", "RCA命中率", sum(s.rca_correct is True for s in rca), len(rca), "ratio"),
        (
            "runbook_hit_rate",
            "Runbook命中率",
            sum(s.runbook_hit for s in runbooks),
            len(runbooks),
            "ratio",
        ),
        (
            "automatic_success_rate",
            "自动处理成功率",
            sum(s.execution_verified for s in executions),
            len(executions),
            "ratio",
        ),
        (
            "approval_rejection_rate",
            "审批拒绝率",
            sum(s.approval_rejections for s in samples),
            sum(s.approval_decisions for s in samples),
            "ratio",
        ),
        (
            "human_takeover_rate",
            "人工接管率",
            sum(s.human_takeover for s in samples),
            len(samples),
            "ratio",
        ),
        (
            "false_alert_rate",
            "误报率",
            sum(s.false_alert is True for s in alerts),
            len(alerts),
            "ratio",
        ),
        ("mean_mttr", "平均MTTR", sum(mttr), len(mttr), "seconds"),
        (
            "mean_tool_calls",
            "平均Tool Call",
            sum(s.tool_calls for s in samples),
            len(samples),
            "calls",
        ),
        (
            "verification_failure_rate",
            "验证失败率",
            sum(s.verification_failures for s in samples),
            sum(s.verification_attempts for s in samples),
            "ratio",
        ),
        (
            "automation_coverage",
            "自动化覆盖率",
            sum(s.automated for s in samples),
            len(samples),
            "ratio",
        ),
    )
    return MetricsReport(
        samples=samples,
        metrics=tuple(
            MetricValue(
                name=name,
                label=label,
                numerator=float(numerator),
                denominator=denominator,
                value=numerator / denominator if denominator else None,
                unit=unit,
            )
            for name, label, numerator, denominator, unit in entries
        ),
    )


class EvaluationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.ledger = LedgerService(session)

    async def label(self, task_id: UUID, label: EvaluationLabel, *, actor: str) -> UUID:
        label = EvaluationLabel.model_validate(label)
        if not self.session.in_transaction():
            raise RuntimeError("人工基准必须在事务内保存")
        task = await self.session.scalar(
            select(AITask).where(AITask.id == task_id).with_for_update()
        )
        if task is None:
            raise ValueError("标注任务不存在")
        if label.false_alert is not None and task.source is not TaskSource.ALERT:
            raise ValueError("只有 Alert 任务可标注误报")
        for evidence_id in label.evidence_ids:
            if (await self.ledger.get_evidence(evidence_id)).task_id != task_id:
                raise ValueError("人工标注不能引用其他任务")
        saved = await self.ledger.append_evidence(
            task_id=task_id,
            source_tool="evaluation.label",
            parameters={"actor": actor.strip()},
            result_snapshot=json_object(label.model_dump(mode="json")),
        )
        await self.ledger.append_audit(
            task_id=task_id,
            actor=actor,
            event_type=AuditEventType.HUMAN_INTERACTION,
            operation="evaluation.label",
            outcome="recorded",
            evidence_id=saved.id,
            details={},
        )
        return saved.id

    async def report(self, window: EvaluationWindow) -> MetricsReport:
        window = EvaluationWindow.model_validate(window)
        tasks = list(
            await self.session.scalars(
                select(AITask)
                .where(AITask.created_at >= window.start, AITask.created_at < window.end)
                .order_by(AITask.created_at, AITask.id)
            )
        )
        samples = []
        for task in tasks:
            event = await self.session.scalar(select(OpsEvent).where(OpsEvent.task_id == task.id))
            if event is not None and event.origin == "learning":
                continue  # 改进任务不重复作为事故样本，避免复盘放大分母。
            history = list(
                await self.session.scalars(
                    select(TaskStatusHistory)
                    .where(
                        TaskStatusHistory.task_id == task.id,
                        TaskStatusHistory.changed_at < window.end,
                    )
                    .order_by(TaskStatusHistory.sequence)
                )
            )
            if not history or history[-1].to_status not in {
                TaskStatus.CLOSED,
                TaskStatus.FAILED,
                TaskStatus.ESCALATED,
                TaskStatus.AUTOMATION_ABORTED,
            }:
                continue  # 未结束工作不伪装为失败或已恢复。
            states = {h.to_status for h in history}
            records = [
                e
                for e in await self.ledger.evidence_for_task(task.id)
                if available(e, window.end) and e.created_at < window.end
            ]
            audits = [
                a
                for a in await self.ledger.audits_for_task(task.id)
                if a.occurred_at < window.end
                and a.created_at < window.end
                and a.details.get("mode") != "replay"
                and not a.actor.startswith("replay:")
            ]
            accepted = {a.evidence_id for a in audits if a.outcome == "succeeded"}
            labels = [
                e
                for e in records
                if e.source_tool == "evaluation.label"
                and any(
                    a.evidence_id == e.id
                    and a.operation == "evaluation.label"
                    and a.outcome == "recorded"
                    for a in audits
                )
            ]
            label_record = max(labels, key=lambda e: (e.created_at, str(e.id))) if labels else None
            label = (
                EvaluationLabel.model_validate_json(json.dumps(label_record.result_snapshot))
                if label_record
                else None
            )
            conclusions = [e for e in records if e.source_tool == "agent.conclusion"]
            conclusion = (
                max(conclusions, key=lambda e: (e.created_at, str(e.id))) if conclusions else None
            )
            matched = [e for e in records if e.source_tool == "runbook.match"]
            runbook_hit = any(
                isinstance(e.result_snapshot, dict)
                and bool(e.result_snapshot.get("runbook_json"))
                and e.result_snapshot.get("blocked") is False
                for e in matched
            )
            decisions = {
                a.evidence_id: a
                for a in audits
                if a.event_type is AuditEventType.APPROVAL
                and a.operation == "approval.decide"
                and a.outcome in {"approved", "rejected"}
            }
            verified = [
                VerificationReport.model_validate_json(json.dumps(e.result_snapshot))
                for e in records
                if e.source_tool == "verify_action"
                and e.id in accepted
                and any(
                    a.evidence_id == e.id
                    and a.actor == "verifier"
                    and a.event_type is AuditEventType.TOOL_CALL
                    for a in audits
                )
            ]
            # 每个验证阶段最多一条，不把重投或 Agent 的只读报告计入独立验证。
            verification = {v.spec.verifying_version: v for v in verified}
            resolved = [h.changed_at for h in history if h.to_status is TaskStatus.RESOLVED]
            takeover = bool(states & {TaskStatus.ESCALATED, TaskStatus.AUTOMATION_ABORTED})
            executed = any(e.source_tool == "execute_action" and e.id in accepted for e in records)
            attempted = executed or any(
                a.event_type is AuditEventType.TOOL_CALL
                and a.operation == "execute_action"
                and a.outcome == "failed"
                for a in audits
            )
            completed = bool(resolved) and any(v.passed for v in verification.values())
            origin = event.occurred_at if event else history[0].changed_at
            samples.append(
                MetricSample(
                    task_id=task.id,
                    rca_correct=(
                        root_cause_hit(
                            AgentConclusion.model_validate_json(
                                json.dumps(conclusion.result_snapshot)
                            ).root_cause.statement,
                            label,
                        )
                        if conclusion
                        else None
                    ),
                    runbook_attempted=bool(matched),
                    runbook_hit=runbook_hit,
                    execution_attempted=attempted,
                    execution_verified=executed and completed and not takeover,
                    approval_decisions=len(decisions),
                    approval_rejections=sum(a.outcome == "rejected" for a in decisions.values()),
                    human_takeover=takeover,
                    alert=task.source is TaskSource.ALERT,
                    false_alert=label.false_alert if label else None,
                    mttr_seconds=max(0.0, (resolved[0] - origin).total_seconds())
                    if resolved
                    else None,
                    tool_calls=sum(
                        a.event_type is AuditEventType.TOOL_CALL and a.details.get("mode") == "live"
                        for a in audits
                    ),
                    verification_attempts=len(verification),
                    verification_failures=sum(not v.passed for v in verification.values()),
                    automated=completed
                    and not takeover
                    and not bool(
                        states & {TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION}
                    ),
                    evidence_ids=tuple(
                        e.id for e in records if not e.source_tool.startswith("replay.")
                    ),
                    history_ids=tuple(h.id for h in history),
                )
            )
        return calculate(tuple(samples))
