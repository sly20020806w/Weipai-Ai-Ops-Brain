"""独立读回权限与工单；聚合报告不能替代真实查询、执行和审计。"""

import json
from dataclasses import asdict
from uuid import UUID

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.activities import lock_task
from app.executor.ticket_models import PermissionGrant, PermissionState, TicketCommand
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.models import RiskLevel
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.safety.service import require_automation_active
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor
from app.tasks.tickets.models import TicketVerifyRequest, TicketVerifyResult
from app.tasks.tickets.service import TicketStore, cached
from app.tasks.workflow_models import TaskSnapshot
from app.tools.models import DispatchStatus, ToolModel
from app.tools.ops_platform import TicketList, TicketQuery
from app.tools.registry import json_object
from app.verifier.authority import _verification_scope


class TicketVerification(ToolModel):
    task_id: UUID
    verifying_version: int = Field(ge=1)
    plan_evidence_id: UUID
    final: bool
    passed: bool
    permission_evidence_id: UUID
    ticket_evidence_id: UUID


async def validate_ticket_report(
    session: AsyncSession,
    task: AITask,
    evidence: Evidence,
    *,
    final: bool,
    require_passed: bool = True,
) -> TicketVerification:
    report = TicketVerification.model_validate_json(json.dumps(evidence.result_snapshot))
    source = "verify_ticket" if final else "verify_ticket_permission"
    if (
        evidence.task_id != task.id
        or evidence.source_tool != source
        or report.task_id != task.id
        or report.verifying_version != task.status_version
        or report.final != final
        or (require_passed and not report.passed)
        or evidence.parameters
        != {
            "task": {
                "task_id": str(task.id),
                "status": "VERIFYING",
                "version": task.status_version,
            },
            "plan_evidence_id": str(report.plan_evidence_id),
            "final": final,
        }
    ):
        raise PermissionError("工单独立验证未通过或版本不匹配")
    ledger = LedgerService(session)
    plan_record = await ledger.get_evidence(report.plan_evidence_id)
    plan = ActionPlan.model_validate_json(json.dumps(plan_record.result_snapshot))
    if plan_record.task_id != task.id or plan_record.source_tool != "action_plan":
        raise PermissionError("验证计划不属于本任务")
    grant = PermissionGrant.model_validate_json(
        json.dumps(plan.actions[0].action.parameters["grant"])
    )
    permission = await ledger.get_evidence(report.permission_evidence_id)
    ticket = await ledger.get_evidence(report.ticket_evidence_id)
    expected_query = {
        "service_name": plan.actions[0].action.service_name,
        "subject_id": grant.subject_id,
        "resource": grant.resource,
    }
    state = PermissionState.model_validate_json(json.dumps(permission.result_snapshot))
    source_ticket = TicketList.model_validate_json(json.dumps(ticket.result_snapshot)).tickets[0]
    if (
        permission.source_tool != "query_ticket_permission"
        or permission.parameters != expected_query
        or state.service_name != expected_query["service_name"]
        or state.subject_id != grant.subject_id
        or state.resource != grant.resource
        or ticket.source_tool != "query_ops_tickets"
        or ticket.parameters
        != TicketQuery(ticket_id=str(plan.actions[0].action.parameters["ticket_id"])).model_dump(
            mode="json"
        )
        or source_ticket.id != plan.actions[0].action.parameters["ticket_id"]
        or source_ticket.service_name != state.service_name
    ):
        raise PermissionError("读回的权限或工单事实不满足批准目标")
    expected_passed = state.grant == grant and source_ticket.status == (
        "closed" if final else "open"
    )
    if report.passed != expected_passed:
        raise PermissionError("工单验证结论与独立事实不一致")
    if (
        final
        and report.passed
        and (not source_ticket.resolution or "Evidence:" not in source_ticket.resolution)
    ):
        raise PermissionError("关闭工单没有证据回填")
    audits = await ledger.audits_for_task(task.id)
    for fact in (permission, ticket, evidence):
        if (
            fact.task_id != task.id
            or fact.collected_at > evidence.collected_at
            or not any(
                a.event_type is AuditEventType.TOOL_CALL
                and a.actor == "verifier"
                and a.operation == fact.source_tool
                and a.evidence_id == fact.id
                and a.outcome == "succeeded"
                and a.details.get("mode") == "live"
                for a in audits
            )
        ):
            raise PermissionError("工单恢复证据缺少独立查询审计")
    records = await ledger.evidence_for_task(task.id)
    for index in range(2 if final else 1):
        commands = [
            e
            for e in records
            if e.source_tool == "execute_action"
            and e.parameters.get("plan_evidence_id") == str(plan_record.id)
            and e.parameters.get("action_id") == plan.actions[index].action.id
        ]
        if len(commands) != 1 or not any(
            a.event_type is AuditEventType.EXECUTION
            and a.evidence_id == commands[0].id
            and a.operation == plan.actions[index].action.name
            and a.outcome == "succeeded"
            for a in audits
        ):
            raise PermissionError("独立验证缺少批准动作的真实执行证据")
        command = TicketCommand.model_validate_json(json.dumps(commands[0].parameters))
        if (
            final
            and report.passed
            and index == 1
            and source_ticket.resolution != command.resolution
        ):
            raise PermissionError("工单回填与批准后的执行内容不一致")
    return report


class TicketVerifier(TicketStore):
    async def verify(self, request: TicketVerifyRequest) -> TicketVerifyResult:
        key = json_object(asdict(request))
        source = "verify_ticket" if request.final else "verify_ticket_permission"
        async with self.database.session() as session, session.begin():
            ledger = LedgerService(session)
            previous = await cached(session, UUID(request.task.task_id), source, key)
            if previous:
                report = TicketVerification.model_validate_json(
                    json.dumps(previous.result_snapshot)
                )
                return TicketVerifyResult(
                    TaskSnapshot(
                        request.task.task_id,
                        TaskStatus.RESOLVED
                        if request.final and report.passed
                        else TaskStatus.INVESTIGATING
                        if not report.passed
                        else TaskStatus.VERIFYING,
                        request.task.version + (1 if request.final or not report.passed else 0),
                    ),
                    str(previous.id),
                    report.passed,
                )
            task = await lock_task(session, request.task)
            if task.status is not TaskStatus.VERIFYING:
                raise ValueError("工单验证必须位于 VERIFYING")
            await require_automation_active(session, task.id)
            record = await ledger.get_evidence(UUID(request.plan_evidence_id))
            plan = ActionPlan.model_validate_json(json.dumps(record.result_snapshot))
            if record.task_id != task.id or record.source_tool != "action_plan":
                raise ValueError("验证计划不属于当前工单")
            action = plan.actions[0].action
            grant = PermissionGrant.model_validate_json(json.dumps(action.parameters["grant"]))
            dispatcher = self.dispatcher(session)

            async def inspect(_: TicketVerifyInput) -> TicketVerification:
                permission_id, permission = await self.read(
                    session,
                    task.id,
                    "query_ticket_permission",
                    {
                        "service_name": action.service_name,
                        "subject_id": grant.subject_id,
                        "resource": grant.resource,
                    },
                    "verifier",
                )
                ticket_id, tickets = await self.read(
                    session,
                    task.id,
                    "query_ops_tickets",
                    {"ticket_id": action.parameters["ticket_id"]},
                    "verifier",
                )
                state = PermissionState.model_validate_json(json.dumps(permission))
                ticket = TicketList.model_validate_json(json.dumps(tickets)).tickets[0]
                passed = (
                    state.grant == grant
                    and ticket.service_name == action.service_name
                    and ticket.status == ("closed" if request.final else "open")
                )
                return TicketVerification(
                    task_id=task.id,
                    verifying_version=task.status_version,
                    plan_evidence_id=record.id,
                    final=request.final,
                    passed=passed,
                    permission_evidence_id=permission_id,
                    ticket_evidence_id=ticket_id,
                )

            # 聚合也经同一个 Dispatcher 留证；事实查询是独立 verifier 身份。
            dispatcher._registry.register(
                name=source,
                description="独立验证批准权限与工单关闭结果",
                input_model=TicketVerifyInput,
                output_model=TicketVerification,
                handler=inspect,
                risk_level=RiskLevel.L0,
            )
            result = await dispatcher.dispatch(
                task_id=task.id, tool_name=source, parameters=key, actor="verifier"
            )
            if result.status is not DispatchStatus.SUCCEEDED or result.evidence_id is None:
                raise ValueError("工单独立验证被 Policy 拒绝")
            report = TicketVerification.model_validate_json(json.dumps(result.result))
            if request.final or not report.passed:
                from app.runbooks.lifecycle import RunbookLifecycle

                with _verification_scope(task.id, task.status_version, result.evidence_id):
                    await RunbookLifecycle(
                        session, self.settings.runbook_maturity_config
                    ).record_verification(task, result.evidence_id)
            if report.passed:
                await validate_ticket_report(
                    session,
                    task,
                    await ledger.get_evidence(result.evidence_id),
                    final=request.final,
                )
            if not report.passed:
                updated = await TaskService(session).transition(
                    task.id,
                    TaskStatus.INVESTIGATING,
                    expected_status=task.status,
                    expected_version=task.status_version,
                    reason=f"工单独立验证未恢复；证据 {result.evidence_id}",
                    actor=TransitionActor.VERIFIER,
                )
            elif request.final:
                with _verification_scope(task.id, task.status_version, result.evidence_id):
                    updated = await TaskService(session).transition(
                        task.id,
                        TaskStatus.RESOLVED,
                        expected_status=task.status,
                        expected_version=task.status_version,
                        reason=f"权限与工单回填关闭独立验证通过；证据 {result.evidence_id}",
                        actor=TransitionActor.VERIFIER,
                    )
            else:
                updated = task
            return TicketVerifyResult(
                TaskSnapshot(str(updated.id), updated.status, updated.status_version),
                str(result.evidence_id),
                report.passed,
            )


class TicketTaskInput(ToolModel):
    task_id: UUID
    status: TaskStatus
    version: int


class TicketVerifyInput(ToolModel):
    task: TicketTaskInput
    plan_evidence_id: UUID
    final: bool
