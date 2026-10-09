"""工单完成后的学习快照与待审核 Runbook，不提升权限或成熟度。"""

import json

from app.agent.activities import lock_task
from app.ledger.service import LedgerService
from app.policy.models import RiskLevel
from app.runbooks.embedding import embedding_client
from app.runbooks.schemas import (
    AutomationLevel,
    DiagnosticStep,
    HandlingStep,
    RunbookCondition,
    RunbookDraft,
    RunbookMaturity,
)
from app.runbooks.service import RunbookService
from app.tasks.planning.models import ActionPlan
from app.tasks.states import TaskStatus
from app.tasks.tickets.models import TicketStageRequest
from app.tasks.tickets.service import TicketStore, cached, stage_key


class TicketLearning(TicketStore):
    async def learn(self, request: TicketStageRequest) -> str:
        async with self.database.session() as session, session.begin():
            task = await lock_task(session, request.task)
            if task.status is not TaskStatus.LEARNING:
                raise ValueError("工单学习必须在 LEARNING")
            key = stage_key(request)
            ledger = LedgerService(session)
            old = await cached(session, task.id, "ticket.learning", key)
            if old:
                return str(old.id)
            records = await ledger.evidence_for_task(task.id)
            verified = [e for e in records if e.source_tool == "verify_ticket"]
            if (
                len(verified) != 1
                or not isinstance(verified[0].result_snapshot, dict)
                or verified[0].result_snapshot.get("passed") is not True
            ):
                raise ValueError("未完成独立工单验证不得学习")
            plans = [e for e in records if e.source_tool == "action_plan"]
            plan = ActionPlan.model_validate_json(json.dumps(plans[-1].result_snapshot))
            grant = plan.actions[0].action.parameters["grant"]
            assert isinstance(grant, dict)
            draft = RunbookDraft(
                name=f"ticket-permission-{task.id.hex}",
                description="权限工单分类、补 Context、检查完整性、审批、独立验证"
                "和带证据关闭的经验；"
                "必须重新核对每次申请。",
                source=f"ai-task:{task.id}; Evidence:{verified[0].id}",
                applicability_conditions=(
                    RunbookCondition(field="task_source", operator="equals", value="Ticket"),
                    RunbookCondition(field="title", operator="contains", value="权限"),
                ),
                exclusion_conditions=(),
                diagnostic_steps=(
                    DiagnosticStep(
                        description="重新读取申请用户与资源的现有权限",
                        tool_name="query_ticket_permission",
                        parameters={
                            "service_name": "$service_name",
                            "subject_id": grant["subject_id"],
                            "resource": grant["resource"],
                        },
                        risk_level=RiskLevel.L0,
                    ),
                ),
                handling_steps=(
                    HandlingStep(
                        description="只在精确审批后授予权限；独立验证后回填关闭",
                        risk_level=RiskLevel.L4,
                    ),
                ),
                risk_level=RiskLevel.L4,
                rollback_plan="验证失败停止关闭，转人工后另行审批撤销权限或重新打开工单。",
                verification_steps=(
                    "逐字段读回用户/资源/权限/有效期",
                    "读回关闭状态与全部证据引用",
                ),
                success_count=0,
                failure_count=0,
                confidence=0.0,
                automation_level=AutomationLevel.MANUAL,
                maturity=RunbookMaturity.DRAFT,
            )
            runbook = await RunbookService(
                session, lambda value: embedding_client(self.settings, value)
            ).create(draft)
            report = await ledger.append_evidence(
                task_id=task.id,
                source_tool="ticket.learning",
                parameters=key,
                result_snapshot={
                    "summary": "本次工单已完成审批、执行、独立验证和回填，经验进入待审核草稿。",
                    "evidence_ids": [
                        str(verified[0].id),
                        str(plan.conclusion_evidence_id),
                        str(plan.review_evidence_id),
                    ],
                    "runbook_draft_id": str(runbook.id),
                },
            )
            return str(report.id)
