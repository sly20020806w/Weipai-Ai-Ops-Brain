"""PLANNING 的复核门禁由 tasks 拥有，不能只依赖 Workflow 的分支。"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.investigation import AgentConclusion
from app.agent.models import ChatResponse
from app.agent.reviewer.models import (
    REVIEW_ACTOR,
    ReviewDecision,
    ReviewReport,
    adjusted_conclusion,
)
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.states import InvalidTaskTransition, TaskSource, TaskStatus
from app.tools.models import DispatchResult, DispatchStatus
from app.triggers.models import OpsEvent


class ReviewRequired(InvalidTaskTransition):
    pass


async def require_review_for_planning(session: AsyncSession, task: AITask) -> None:
    from app.tasks.war_room.service import require_war_room_review

    if await require_war_room_review(session, task) is not None:
        return
    from app.tasks.releases.service import require_release_review

    if await require_release_review(session, task):
        return
    from app.tasks.tickets.service import require_ticket_review

    await require_ticket_review(session, task)
    evidence = await LedgerService(session).evidence_for_task(task.id)
    conclusions = [item for item in evidence if item.source_tool == "agent.conclusion"]
    origin = await session.scalar(select(OpsEvent.origin).where(OpsEvent.task_id == task.id))
    # 现有任务没有 severity 字段，保守覆盖全部告警、发布、变更事件和真实 Agent 结论。
    if (
        not conclusions
        and task.source not in {TaskSource.ALERT, TaskSource.RELEASE}
        and origin
        not in {
            "git",
            "ci",
            "argocd",
            "config_center",
            "cloud",
        }
    ):
        return
    history = await session.scalars(
        select(TaskStatusHistory)
        .where(TaskStatusHistory.task_id == task.id)
        .order_by(TaskStatusHistory.sequence.desc())
    )
    rca = next(
        (item for item in history if item.to_status in {TaskStatus.RCA, TaskStatus.INVESTIGATING}),
        None,
    )
    if rca is None or rca.to_status is not TaskStatus.RCA:
        raise ReviewRequired("关键任务必须先完成当前 RCA 的 Reviewer 复核")
    current = [item for item in conclusions if item.parameters.get("phase_version") == rca.sequence]
    reviews = [
        item
        for item in evidence
        if item.source_tool == "reviewer.verdict"
        and item.parameters.get("phase_version") == rca.sequence
    ]
    if len(current) != 1 or len(reviews) != 1:
        raise ReviewRequired("关键任务缺少唯一的当前结论和复核记录")
    try:
        conclusion = AgentConclusion.model_validate_json(json.dumps(current[0].result_snapshot))
        review = ReviewDecision.model_validate_json(json.dumps(reviews[0].result_snapshot))
        think = [
            item
            for item in evidence
            if item.source_tool == "reviewer.think"
            and item.parameters.get("phase_version") == rca.sequence
            and item.parameters.get("step") == review.steps
        ]
        if len(think) != 1:
            raise ValueError("复核缺少最终模型检查点")
        response = ChatResponse.model_validate_json(json.dumps(think[0].result_snapshot))
        report = ReviewReport.model_validate_json(response.message.content or "{}")
        observations = sorted(
            (
                item
                for item in evidence
                if item.source_tool == "reviewer.observe"
                and item.parameters.get("phase_version") == rca.sequence
            ),
            key=lambda item: int(str(item.parameters["step"])),
        )
        observed = [
            DispatchResult.model_validate_json(json.dumps(item.result_snapshot))
            for item in observations
        ]
        if (
            review.report.verdict != "clear"
            or review.report != report
            or response.finish_reason != "stop"
            or response.message.refusal is not None
            or response.message.tool_calls
            or review.conclusion_evidence_id != current[0].id
            or reviews[0].parameters.get("conclusion_evidence_id") != str(current[0].id)
            or review.original_confidence != conclusion.confidence
            or review.conclusion != adjusted_conclusion(conclusion, report)
            or not report.evidence_ids <= set(review.observed_ids)
            or list(review.observed_ids)
            != [item.evidence_id for item in observed if item.status is DispatchStatus.SUCCEEDED]
        ):
            raise ValueError("当前结论的复核不允许进入计划")
        ledger = LedgerService(session)
        audits = await ledger.audits_for_task(task.id)
        for reference in report.evidence_ids:
            referenced = await ledger.get_evidence(reference)
            if referenced.task_id != task.id or not any(
                audit.event_type is AuditEventType.TOOL_CALL
                and audit.actor == REVIEW_ACTOR
                and audit.evidence_id == reference
                and audit.operation == referenced.source_tool
                and audit.outcome == "succeeded"
                for audit in audits
            ):
                raise ValueError("复核引用缺少真实查询审计")
    except (ValueError, LookupError):
        raise ReviewRequired("Reviewer 发现反证、覆盖不足或当前复核证据无效") from None
