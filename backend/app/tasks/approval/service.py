"""只追加审批单、决定与审计，同任务行锁保证并发和提交后重试幂等。"""

import json
from dataclasses import asdict
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.feishu.base import FeishuConnector
from app.connectors.feishu.models import CardButton, CardNotification, InteractiveCard
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyAction, PolicyDecision
from app.tasks.approval.models import ApprovalTicket, action_hash, approval_id, validate_response
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.review_gate import require_review_for_planning
from app.tasks.states import TaskStatus
from app.tasks.workflow_models import (
    ApprovalDecisionRequest,
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResult,
    TaskSnapshot,
)
from app.tools.registry import json_object


def make_ticket(request: ApprovalRequest, plan: ActionPlan) -> ApprovalTicket:
    if (
        request.task.status is not TaskStatus.WAITING_APPROVAL
        or type(request.task.version) is not int
        or request.task.version != plan.planning_version + 1
        or str(plan.task_id) != request.task.task_id
        or plan.decision is not PolicyDecision.NEED_APPROVAL
    ):
        raise ValueError("审批单必须对应当前等待审批的计划")
    digest = action_hash(plan)
    return ApprovalTicket(
        approval_id=UUID(approval_id(request.task, request.plan_evidence_id, digest)),
        task_id=plan.task_id,
        wait_version=request.task.version,
        plan_evidence_id=UUID(request.plan_evidence_id),
        action_hash=digest,
        plan=plan,
    )


def approval_card(ticket: ApprovalTicket) -> CardNotification:
    buttons: list[tuple[str, Literal["primary", "danger"], str]] = [
        ("批准", "primary", "approved"),
        ("拒绝", "danger", "rejected"),
    ]
    lines = [
        f"任务：{ticket.task_id}",
        f"审批单：{ticket.approval_id}",
        f"计划证据：{ticket.plan_evidence_id}",
        f"目标环境：{ticket.plan.environment.value}",
        f"动作哈希：{ticket.action_hash}",
    ]
    for item in ticket.plan.actions:
        action = item.action
        lines.extend(
            [
                f"\n动作：{action.id} / {action.name} / {action.service_name}"
                f" / {action.risk_level.value}",
                "参数：" + json.dumps(action.parameters, ensure_ascii=False, sort_keys=True),
                "前置条件：" + "；".join(action.preconditions),
                f"回滚：{action.rollback.description}",
                "回滚参数："
                + json.dumps(action.rollback.parameters, ensure_ascii=False, sort_keys=True),
                f"回滚触发：{action.rollback.trigger}",
                "验证：" + "；".join(action.verification.checks),
                f"成功标准：{action.verification.success_criteria}",
                f"失败处理：{action.verification.failure_response}",
            ]
        )
    # 不截断参数或风险描述；超卡片上限时失败转人工，避免批准未展示的内容。
    return CardNotification(
        notification_id=ticket.approval_id,
        card=InteractiveCard(
            title="需要你的动作审批",
            markdown="\n".join(lines),
            buttons=tuple(
                CardButton(
                    label=label,
                    style=style,
                    value={
                        "operation": "action_approval",
                        "task_id": str(ticket.task_id),
                        "approval_id": str(ticket.approval_id),
                        "wait_version": ticket.wait_version,
                        "action_hash": ticket.action_hash,
                        "decision": decision,
                    },
                )
                for label, style, decision in buttons
            ),
        ),
    )


def prompt_for(ticket: ApprovalTicket, evidence_id: UUID) -> ApprovalPrompt:
    return ApprovalPrompt(
        TaskSnapshot(str(ticket.task_id), TaskStatus.WAITING_APPROVAL, ticket.wait_version),
        str(ticket.approval_id),
        str(evidence_id),
        str(ticket.plan_evidence_id),
        ticket.action_hash,
    )


def check_policy(plan: ActionPlan, settings: Settings) -> None:
    policy = create_policy_engine(settings)
    if policy.environment is not plan.environment or any(
        policy.evaluate(
            PolicyAction(
                name=item.action.name, risk_level=item.action.risk_level, runbook=plan.runbook
            )
        )
        != item.policy
        for item in plan.actions
    ):
        raise ValueError("Policy 已变化，原计划与审批失效")


async def check_runbook(
    session: AsyncSession,
    plan: ActionPlan,
    settings: Settings,
    *,
    lock: bool = True,
) -> None:
    from app.runbooks.lifecycle import task_runbook_context

    current = await task_runbook_context(
        session, plan.task_id, settings.runbook_maturity_config, lock=lock
    )
    if current != plan.runbook:
        raise ValueError("Runbook 内容、审核或成熟度已变化，原计划与审批失效")


async def load_plan(session: AsyncSession, request: ApprovalRequest) -> ActionPlan:
    record = await LedgerService(session).get_evidence(UUID(request.plan_evidence_id))
    plan = ActionPlan.model_validate_json(json.dumps(record.result_snapshot))
    if (
        record.task_id != UUID(request.task.task_id)
        or record.source_tool != "action_plan"
        or record.parameters.get("phase_version") != plan.planning_version
        or record.parameters.get("conclusion_evidence_id") != str(plan.conclusion_evidence_id)
        or record.parameters.get("review_evidence_id") != str(plan.review_evidence_id)
    ):
        raise ValueError("审批计划不是本任务的真实动作证据")
    return plan


async def decision_evidence(session: AsyncSession, prompt: ApprovalPrompt) -> Evidence | None:
    record: Evidence | None = await session.scalar(
        select(Evidence).where(
            Evidence.task_id == UUID(prompt.task.task_id),
            Evidence.source_tool == "approval.decision",
            Evidence.parameters["prompt"]["approval_id"].as_string() == prompt.approval_id,
        )
    )
    return record


class ApprovalStore:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    async def is_approved(self, prompt: ApprovalPrompt, current_plan: ActionPlan) -> bool:
        """供后续 Executor 核对授权；哈希、状态版本、原证据与 Policy 都须仍有效。"""
        if action_hash(current_plan) != prompt.action_hash:
            return False
        async with self.database.session() as session:
            from app.tasks.takeover import takeover_record

            if await takeover_record(session, UUID(prompt.task.task_id)) is not None:
                return False
            task = await session.get(AITask, UUID(prompt.task.task_id))
            if task is None or (task.status, task.status_version) not in {
                (TaskStatus.WAITING_APPROVAL, prompt.task.version),
                (TaskStatus.EXECUTING, prompt.task.version + 1),
            }:
                return False
            try:
                original = await load_plan(
                    session, ApprovalRequest(prompt.task, prompt.plan_evidence_id)
                )
                if original != current_plan:
                    return False
                check_policy(original, self.settings)
                await check_runbook(session, original, self.settings, lock=False)
                # 执行前仍以原等待版本验收，避免将审批扩展到其他阶段或版本。
                records = await LedgerService(session).evidence_for_task(task.id)
                ticket = make_ticket(
                    ApprovalRequest(prompt.task, prompt.plan_evidence_id), original
                )
                request = await LedgerService(session).get_evidence(
                    UUID(prompt.request_evidence_id)
                )
                if (
                    request not in records
                    or request.source_tool != "approval.request"
                    or request.result_snapshot != ticket.model_dump(mode="json")
                    or prompt_for(ticket, request.id) != prompt
                ):
                    return False
                decision = await decision_evidence(session, prompt)
                audits = await LedgerService(session).audits_for_task(task.id)
                return (
                    decision is not None
                    and isinstance(decision.result_snapshot, dict)
                    and decision.result_snapshot.get("decision") == "approved"
                    and decision.result_snapshot.get("action_hash") == prompt.action_hash
                    and any(
                        a.evidence_id == decision.id
                        and a.event_type is AuditEventType.APPROVAL
                        and a.operation == "approval.decide"
                        and a.outcome == "approved"
                        and a.actor == decision.result_snapshot.get("actor")
                        for a in audits
                    )
                )
            except (ValueError, LookupError):
                return False

    async def notify(self, request: ApprovalRequest, connector: FeishuConnector) -> ApprovalPrompt:
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == UUID(request.task.task_id)).with_for_update()
            )
            if task is None:
                raise ValueError("审批任务不存在")
            plan = await load_plan(session, request)
            ticket = make_ticket(request, plan)
            ledger = LedgerService(session)
            cached = await session.scalar(
                select(Evidence).where(
                    Evidence.task_id == task.id,
                    Evidence.source_tool == "approval.request",
                    Evidence.parameters["task"]["version"].as_integer() == request.task.version,
                )
            )
            if cached is not None:
                if cached.parameters != json_object(
                    asdict(request)
                ) or cached.result_snapshot != ticket.model_dump(mode="json"):
                    raise ValueError("同一等待版本不能改写审批单")
                return prompt_for(ticket, cached.id)
            if (task.status, task.status_version) != (request.task.status, request.task.version):
                raise ValueError("审批等待状态已过期")
            await require_review_for_planning(session, task)
            check_policy(plan, self.settings)
            await check_runbook(session, plan, self.settings)
            card = approval_card(ticket)
            receipt = await connector.send(card)
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="approval.request",
                parameters=json_object(asdict(request)),
                result_snapshot=json_object(ticket.model_dump(mode="json")),
            )
            await ledger.append_audit(
                task_id=task.id,
                event_type=AuditEventType.APPROVAL,
                actor="workflow",
                operation="approval.request",
                outcome="sent",
                evidence_id=evidence.id,
                details={
                    "approval_id": str(ticket.approval_id),
                    "action_hash": ticket.action_hash,
                    "receipt": json_object(receipt.model_dump(mode="json")),
                },
            )
            return prompt_for(ticket, evidence.id)

    async def decide(self, request: ApprovalDecisionRequest) -> ApprovalResult:
        prompt, response = request.prompt, request.response
        if response is not None:
            validate_response(response)
            if (
                response.task_id,
                response.approval_id,
                response.wait_version,
                response.action_hash,
            ) != (prompt.task.task_id, prompt.approval_id, prompt.task.version, prompt.action_hash):
                raise ValueError("决定与审批单身份或动作哈希不符")
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == UUID(prompt.task.task_id)).with_for_update()
            )
            if task is None:
                raise ValueError("审批任务不存在")
            ledger = LedgerService(session)
            plan = await load_plan(session, ApprovalRequest(prompt.task, prompt.plan_evidence_id))
            ticket = make_ticket(ApprovalRequest(prompt.task, prompt.plan_evidence_id), plan)
            record = await ledger.get_evidence(UUID(prompt.request_evidence_id))
            if (
                record.task_id != task.id
                or record.source_tool != "approval.request"
                or record.parameters
                != json_object(asdict(ApprovalRequest(prompt.task, prompt.plan_evidence_id)))
                or record.result_snapshot != ticket.model_dump(mode="json")
                or prompt_for(ticket, record.id) != prompt
            ):
                raise ValueError("审批单证据被篡改或不属于当前任务")
            payload = json_object(asdict(request))
            cached = await decision_evidence(session, prompt)
            if cached is not None:
                if cached.parameters != payload:
                    raise ValueError("审批单已有其他决定")
                return ApprovalResult(
                    str(cached.id), response.decision if response else "expired", prompt.action_hash
                )
            if (task.status, task.status_version) != (prompt.task.status, prompt.task.version):
                raise ValueError("审批等待状态已过期")
            decision = response.decision if response else "expired"
            # 与 Ledger 的操作人规范化一致，避免首尾空白导致已批准记录无法通过门禁。
            actor = response.actor.strip() if response else "workflow"
            if decision == "approved":
                await require_review_for_planning(session, task)
                check_policy(plan, self.settings)
                await check_runbook(session, plan, self.settings)
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="approval.decision",
                parameters=payload,
                result_snapshot={
                    "approval_id": prompt.approval_id,
                    "action_hash": prompt.action_hash,
                    "decision": decision,
                    "actor": actor,
                },
            )
            await ledger.append_audit(
                task_id=task.id,
                event_type=AuditEventType.APPROVAL,
                actor=actor,
                operation="approval.decide",
                outcome=decision,
                evidence_id=evidence.id,
                details={
                    "approval_id": prompt.approval_id,
                    "action_hash": prompt.action_hash,
                    "plan_evidence_id": prompt.plan_evidence_id,
                    "wait_version": prompt.task.version,
                },
            )
            return ApprovalResult(str(evidence.id), decision, prompt.action_hash)


async def require_approval_for_execution(session: AsyncSession, task: AITask) -> None:
    records = await LedgerService(session).evidence_for_task(task.id)
    plans = [item for item in records if item.source_tool == "action_plan"]
    if not plans:
        return  # 既有纯本地占位流程不拥有真实动作计划。
    current = [
        item
        for item in plans
        if item.parameters.get("phase_version")
        == task.status_version - (1 if task.status is TaskStatus.WAITING_APPROVAL else 0)
    ]
    if len(current) != 1:
        raise ValueError("缺少唯一的当前动作计划")
    plan = ActionPlan.model_validate_json(json.dumps(current[0].result_snapshot))
    if plan.decision is PolicyDecision.DENY:
        raise ValueError("Policy 禁止的计划不能执行")
    if plan.decision is PolicyDecision.ALLOW:
        return
    ticket = make_ticket(
        ApprovalRequest(
            TaskSnapshot(str(task.id), task.status, task.status_version), str(current[0].id)
        ),
        plan,
    )
    requests = [
        item
        for item in records
        if item.source_tool == "approval.request"
        and item.result_snapshot == ticket.model_dump(mode="json")
    ]
    if len(requests) != 1:
        raise ValueError("需要有效的动作审批单")
    prompt = prompt_for(ticket, requests[0].id)
    decision = await decision_evidence(session, prompt)
    if (
        decision is None
        or not isinstance(decision.result_snapshot, dict)
        or decision.result_snapshot.get("decision") != "approved"
        or decision.result_snapshot.get("action_hash") != action_hash(plan)
    ):
        raise ValueError("没有与当前动作哈希绑定的批准记录")
    audits = await LedgerService(session).audits_for_task(task.id)
    if not any(
        item.event_type is AuditEventType.APPROVAL
        and item.evidence_id == decision.id
        and item.outcome == "approved"
        and item.actor == decision.result_snapshot.get("actor")
        for item in audits
    ):
        raise ValueError("批准记录缺少操作人审计")
