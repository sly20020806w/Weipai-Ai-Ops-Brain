"""工单动作复用审批、执行范围、唯一 Dispatcher、熔断和只追加执行意图。"""

import json
from dataclasses import asdict
from uuid import UUID, uuid5

from app.config import Settings
from app.connectors.ops_platform.tickets import FakeTicketWriter, TicketState
from app.db.base import utc_now
from app.db.session import Database
from app.executor.authority import _execution_scope, _ExecutionAuthority
from app.executor.ticket_models import (
    PermissionGrant,
    TicketCommand,
    TicketReceipt,
    ticket_command_hash,
)
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyDecision, RiskLevel
from app.tasks.approval.models import action_hash
from app.tasks.approval.service import (
    check_policy,
    check_runbook,
    decision_evidence,
    load_plan,
    make_ticket,
    prompt_for,
)
from app.tasks.planning.models import ActionPlan
from app.tasks.safety.models import AutomationAborted
from app.tasks.safety.service import SafetyService, SafetyStore, require_automation_active
from app.tasks.states import TaskStatus
from app.tasks.tickets.models import TicketContext, TicketExecutionRequest
from app.tasks.tickets.service import TicketStore, allowed_request, cached, require_ticket_review
from app.tasks.workflow_models import ApprovalRequest
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus, JsonObject
from app.tools.ops_platform import TicketList
from app.tools.registry import ToolRegistry, json_object
from app.tools.tickets import register_ticket_execution


class TicketExecutor(TicketStore):
    def __init__(self, database: Database, settings: Settings, state: TicketState) -> None:
        super().__init__(database, settings, state)
        self.writer = FakeTicketWriter(state)

    def execution_dispatcher(self, session: object) -> ToolDispatcher:
        from sqlalchemy.ext.asyncio import AsyncSession

        assert isinstance(session, AsyncSession)
        registry = ToolRegistry()
        register_ticket_execution(
            registry, self.writer, self.settings.ticket_config.credential_ttl_seconds
        )
        return ToolDispatcher(registry, create_policy_engine(self.settings), LedgerService(session))

    async def execute(self, request: TicketExecutionRequest) -> str:
        from app.agent.activities import lock_task
        from app.verifier.tickets import validate_ticket_report

        key = json_object(asdict(request))
        if (
            not self.settings.ticket_config.enabled
            or not self.settings.execution_config.enabled
            or self.settings.connector_mode.value != "fake"
        ):
            raise PermissionError("工单 Executor 未启用或真实动作端尚未验收")
        if type(request.action_index) is not int or request.action_index not in {0, 1}:
            raise ValueError("工单动作索引无效")
        async with self.database.session() as session, session.begin():
            ledger = LedgerService(session)
            previous = await cached(session, UUID(request.task.task_id), "ticket.execution", key)
            if previous:
                return str(previous.result_snapshot)
            task = await lock_task(session, request.task)
            await require_automation_active(session, task.id)
            record = await ledger.get_evidence(UUID(request.plan_evidence_id))
            plan = ActionPlan.model_validate_json(json.dumps(record.result_snapshot))
            check_policy(plan, self.settings)
            await check_runbook(session, plan, self.settings)
            await require_ticket_review(session, task)
            expected = (
                plan.planning_version
                + (2 if plan.decision is PolicyDecision.NEED_APPROVAL else 1)
                + request.action_index
            )
            if (
                record.task_id != task.id
                or record.source_tool != "action_plan"
                or record.parameters.get("phase_version") != plan.planning_version
                or plan.task_id != task.id
                or task.status_version != expected
                or task.status
                is not (TaskStatus.EXECUTING if request.action_index == 0 else TaskStatus.VERIFYING)
            ):
                raise PermissionError("工单动作不属于当前授权阶段")
            if plan.decision is PolicyDecision.DENY:
                raise PermissionError("Policy 禁止动作")
            if plan.decision is PolicyDecision.NEED_APPROVAL:
                prompt = request.approval
                if (
                    prompt is None
                    or prompt.plan_evidence_id != request.plan_evidence_id
                    or prompt.action_hash != action_hash(plan)
                ):
                    raise PermissionError("工单写动作缺少精确审批")
                original = await load_plan(
                    session, ApprovalRequest(prompt.task, prompt.plan_evidence_id)
                )
                approval_ticket = make_ticket(
                    ApprovalRequest(prompt.task, prompt.plan_evidence_id), original
                )
                approval = await ledger.get_evidence(UUID(prompt.request_evidence_id))
                decision = await decision_evidence(session, prompt)
                audits = await ledger.audits_for_task(task.id)
                if (
                    original != plan
                    or approval.task_id != task.id
                    or approval.source_tool != "approval.request"
                    or approval.result_snapshot != approval_ticket.model_dump(mode="json")
                    or prompt_for(approval_ticket, approval.id) != prompt
                    or decision is None
                    or not isinstance(decision.result_snapshot, dict)
                    or decision.result_snapshot.get("decision") != "approved"
                    or decision.result_snapshot.get("action_hash") != prompt.action_hash
                    or not any(
                        a.event_type is AuditEventType.APPROVAL
                        and a.evidence_id == decision.id
                        and a.outcome == "approved"
                        and a.actor == decision.result_snapshot.get("actor")
                        for a in audits
                    )
                ):
                    raise PermissionError("审批不匹配或缺操作人审计")
            if len(plan.actions) != 2 or tuple(
                (i.action.name, i.action.risk_level) for i in plan.actions
            ) != (("grant_ticket_permission", RiskLevel.L4), ("close_ticket", RiskLevel.L1)):
                raise PermissionError("工单执行仅接受完整两动作计划")
            item = plan.actions[request.action_index]
            action = item.action
            contexts = [
                e
                for e in await ledger.evidence_for_task(task.id)
                if e.source_tool == "ticket.context"
            ]
            context = TicketContext.model_validate_json(json.dumps(contexts[-1].result_snapshot))
            params = {
                "ticket_id": context.ticket_id,
                "grant": json_object(context.request.model_dump(mode="json")),
            }
            if any(
                i.action.parameters != params or i.action.service_name != context.service_name
                for i in plan.actions
            ):
                raise PermissionError("批准目标被改写")
            grant = PermissionGrant.model_validate_json(context.request.model_dump_json())
            _, current_ticket = await self.read(
                session, task.id, "query_ops_tickets", {"ticket_id": context.ticket_id}, "executor"
            )
            current = TicketList.model_validate_json(json.dumps(current_ticket)).tickets[0]
            if (
                current.service_name != context.service_name
                or current.status != "open"
                or not allowed_request(
                    self.settings, context.service_name, current.requester_id, grant
                )
            ):
                raise PermissionError("宿主规则或源工单已变化，原意图不能赋权")
            if current.permission_request is not None and any(
                value is not None and value != getattr(grant, field)
                for field, value in current.permission_request.model_dump().items()
            ):
                raise PermissionError("源工单申请字段已变化，旧授权失效")
            guard = await SafetyService(session, self.settings.safety_config).check(
                task, pending_action=(request.plan_evidence_id, action.id)
            )
            if guard.evidence_id:
                command = None
            else:
                intent_key: JsonObject = {
                    "plan_evidence_id": request.plan_evidence_id,
                    "action_id": action.id,
                }
                intent = await cached(session, task.id, "execution.intent", intent_key)
                resolution = None
                if request.action_index == 1:
                    proofs = [
                        e
                        for e in await ledger.evidence_for_task(task.id)
                        if e.source_tool == "verify_ticket_permission"
                        and e.parameters.get("plan_evidence_id") == request.plan_evidence_id
                    ]
                    if len(proofs) != 1:
                        raise PermissionError("关闭前缺少唯一独立权限验证")
                    await validate_ticket_report(session, task, proofs[0], final=False)
                    resolution = "权限已按审批完成，独立验证通过。Evidence: " + ", ".join(
                        [
                            *(str(i) for i in plan.summary.evidence_ids),
                            str(proofs[0].id),
                            str(plan.conclusion_evidence_id),
                            str(plan.review_evidence_id),
                        ]
                    )
                if intent:
                    command = TicketCommand.model_validate_json(json.dumps(intent.result_snapshot))
                    if (
                        command.plan_hash != action_hash(plan)
                        or command.grant != grant
                        or command.resolution != resolution
                    ):
                        raise PermissionError("已提交意图或验证绑定发生变化")
                else:
                    _, current_snapshot = await self.read(
                        session,
                        task.id,
                        "query_ops_tickets",
                        {"ticket_id": context.ticket_id},
                        "executor",
                    )
                    ticket = TicketList.model_validate_json(json.dumps(current_snapshot)).tickets[0]
                    if ticket.status != "open" or not allowed_request(
                        self.settings, context.service_name, ticket.requester_id, grant
                    ):
                        raise PermissionError("当前工单或宿主权限范围不再允许执行")
                    command = TicketCommand(
                        execution_id=uuid5(task.id, f"{record.id}/{action.id}/{action_hash(plan)}"),
                        task_id=task.id,
                        plan_evidence_id=record.id,
                        plan_hash=action_hash(plan),
                        action_id=action.id,
                        name="grant_ticket_permission"
                        if request.action_index == 0
                        else "close_ticket",
                        service_name=context.service_name,
                        ticket_id=context.ticket_id,
                        expected_updated_at=ticket.updated_at,
                        grant=grant,
                        resolution=resolution,
                    )
                    await ledger.append_evidence(
                        task_id=task.id,
                        source_tool="execution.intent",
                        parameters=intent_key,
                        result_snapshot=json_object(command.model_dump(mode="json")),
                    )
        if command is None:
            raise AutomationAborted("工单写入前已熔断，零签发")
        async with self.database.session() as session, session.begin():
            task = await lock_task(session, request.task)
            await require_automation_active(session, task.id)
            ledger = LedgerService(session)
            previous = await cached(session, task.id, "ticket.execution", key)
            if previous:
                return str(previous.result_snapshot)
            check_policy(plan, self.settings)
            await check_runbook(session, plan, self.settings)
            parameters = json_object(command.model_dump(mode="json"))
            with _execution_scope(
                _ExecutionAuthority(task.id, parameters, item.policy, id(session), plan.runbook)
            ):
                result = await self.execution_dispatcher(session).dispatch(
                    task_id=task.id,
                    tool_name="execute_action",
                    parameters=parameters,
                    actor="executor",
                )
            failed = result.status is not DispatchStatus.SUCCEEDED or result.evidence_id is None
            if not failed:
                receipt = TicketReceipt.model_validate_json(json.dumps(result.result))
                if (
                    receipt.execution_id,
                    receipt.command_hash,
                    receipt.ticket_id,
                    receipt.status,
                ) != (
                    command.execution_id,
                    ticket_command_hash(command),
                    command.ticket_id,
                    "closed" if request.action_index == 1 else "open",
                ):
                    raise ValueError("工单动作回执不匹配")
                await ledger.append_audit(
                    task_id=task.id,
                    event_type=AuditEventType.EXECUTION,
                    actor="executor",
                    operation=command.name,
                    outcome="succeeded",
                    evidence_id=result.evidence_id,
                    details={
                        "plan_hash": command.plan_hash,
                        "execution_id": str(command.execution_id),
                    },
                )
                await ledger.append_evidence(
                    task_id=task.id,
                    source_tool="ticket.execution",
                    parameters=key,
                    result_snapshot=str(result.evidence_id),
                )
                return str(result.evidence_id)
        await SafetyStore(self.database, self.settings.safety_config).check(request.task)
        raise RuntimeError("工单动作失败，保留原幂等意图由 Temporal 重试")

    async def replay(self, request: TicketExecutionRequest, evidence_id: UUID) -> JsonObject:
        async with self.database.session() as session, session.begin():
            ledger = LedgerService(session)
            evidence = await ledger.get_evidence(evidence_id)
            record = await ledger.get_evidence(UUID(request.plan_evidence_id))
            plan = ActionPlan.model_validate_json(json.dumps(record.result_snapshot))
            command = TicketCommand.model_validate_json(json.dumps(evidence.parameters))
            check_policy(plan, self.settings)
            if (
                evidence.task_id != UUID(request.task.task_id)
                or command.plan_hash != action_hash(plan)
                or command.plan_evidence_id != record.id
            ):
                raise ValueError("工单回放计划不匹配")
            item = plan.actions[request.action_index]
            parameters = json_object(command.model_dump(mode="json"))
            with _execution_scope(
                _ExecutionAuthority(
                    evidence.task_id, parameters, item.policy, id(session), plan.runbook
                )
            ):
                result = await self.execution_dispatcher(session).dispatch(
                    task_id=evidence.task_id,
                    tool_name="execute_action",
                    parameters=parameters,
                    actor="executor-replay",
                    mode=DispatchMode.REPLAY,
                    replay_evidence_id=evidence_id,
                    replay_before=utc_now(),
                )
            if result.status is not DispatchStatus.REPLAYED or result.result is None:
                raise ValueError("工单回放被拒绝")
            return result.result
