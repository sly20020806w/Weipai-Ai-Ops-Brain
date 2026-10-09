"""独立发布观测；成功回执只能证明提交，不能证明业务恢复。"""

import json
from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.activities import lock_task
from app.connectors.kubernetes.execution import ExecutionReceipt
from app.executor.models import ExecutionCommand, command_hash
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import RiskLevel
from app.tasks.approval.models import action_hash
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.releases.models import (
    ReleaseObservation,
    ReleaseObserveRequest,
    ReleaseObserveResult,
)
from app.tasks.releases.service import ReleaseStore, configuration_hash, recovery_checks
from app.tasks.safety.service import require_automation_active
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor
from app.tasks.tickets.service import cached
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus, ToolModel
from app.tools.registry import ToolRegistry, json_object
from app.verifier.authority import _verification_scope


class ReleaseVerification(ToolModel):
    task_id: UUID
    verifying_version: int
    plan_evidence_id: UUID
    observation_evidence_id: UUID
    execution_evidence_id: UUID
    final: bool
    target_matches: bool
    checks: dict[str, bool]
    config_hash: str

    @property
    def passed(self) -> bool:
        return (
            self.target_matches
            and set(self.checks)
            == {
                "deployment",
                "pods",
                "http_5xx",
                "p99",
                "success_ratio",
                "logs",
                "traces",
                "resources",
            }
            and all(self.checks.values())
        )


class ReleaseVerifyInput(ToolModel):
    task_id: UUID
    version: int
    plan_evidence_id: UUID
    release_id: str
    start: str
    end: str
    final: bool


async def validate_release_report(
    session: AsyncSession, task: AITask, evidence: Evidence, *, require_passed: bool
) -> ReleaseVerification:
    report = ReleaseVerification.model_validate_json(json.dumps(evidence.result_snapshot))
    if (
        evidence.task_id != task.id
        or evidence.source_tool != "verify_release"
        or report.task_id != task.id
        or report.verifying_version != task.status_version
        or (require_passed and (not report.final or not report.passed))
    ):
        raise PermissionError("发布验证不属于当前阶段，或未独立恢复")
    ledger = LedgerService(session)
    observation = await ledger.get_evidence(report.observation_evidence_id)
    execution = await ledger.get_evidence(report.execution_evidence_id)
    plan_record = await ledger.get_evidence(report.plan_evidence_id)
    plan = ActionPlan.model_validate_json(json.dumps(plan_record.result_snapshot))
    command = ExecutionCommand.model_validate_json(json.dumps(execution.parameters))
    receipt = ExecutionReceipt.model_validate_json(json.dumps(execution.result_snapshot))
    fact = ReleaseObservation.model_validate_json(json.dumps(observation.result_snapshot))
    expected_target = (
        fact.target.cluster_name,
        fact.target.deployment_name,
        fact.target.container_name,
        fact.target.image,
        fact.target.replicas,
        fact.target.uid,
        fact.target.namespace,
        fact.target.paused,
        fact.target.traffic_percent,
    )
    receipt_target = (
        receipt.target.cluster_name,
        receipt.target.deployment_name,
        receipt.target.container_name,
        receipt.target.image,
        receipt.target.replicas,
        receipt.target.uid,
        receipt.target.namespace,
        receipt.target.paused,
        receipt.target.traffic_percent,
    )
    params = evidence.parameters
    if (
        plan_record.task_id != task.id
        or plan_record.source_tool != "action_plan"
        or command.plan_evidence_id != plan_record.id
        or command.task_id != task.id
        or command.plan_hash != action_hash(plan)
        or receipt.command_hash != command_hash(command)
        or receipt.execution_id != command.execution_id
        or report.verifying_version
        != plan.planning_version + (3 if plan.decision.value == "need_approval" else 2)
        or execution.source_tool != "execute_action"
        or observation.source_tool != "query_release_observation"
        or observation.parameters
        != {
            "release_id": fact.release_id,
            "start": fact.start.isoformat().replace("+00:00", "Z"),
            "end": fact.end.isoformat().replace("+00:00", "Z"),
        }
        or str(report.plan_evidence_id) != params.get("plan_evidence_id")
        or params.get("task_id") != str(task.id)
        or params.get("version") != task.status_version
        or params.get("release_id") != fact.release_id
        or datetime.fromisoformat(str(params.get("start"))) != fact.start
        or datetime.fromisoformat(str(params.get("end"))) != fact.end
        or params.get("final") != report.final
        or fact.service_name != plan.actions[0].action.service_name
        or fact.start < receipt.completed_at
        or fact.end > observation.collected_at
        or report.target_matches != (expected_target == receipt_target)
        or (
            report.passed
            and (
                not fact.deployment_ready
                or not fact.pods_ready
                or not fact.logs_healthy
                or not fact.traces_healthy
                or not fact.resources_healthy
                or any(
                    len(series) < 3 for series in (fact.http_5xx, fact.p99_ms, fact.success_ratio)
                )
            )
        )
        or (
            report.final
            and report.passed
            and (fact.target.paused or fact.target.traffic_percent != 100)
        )
    ):
        raise PermissionError("发布验证事实与批准回执、目标或时间窗不符")
    audits = await ledger.audits_for_task(task.id)
    for record, actor, event_type in (
        (observation, "verifier", AuditEventType.TOOL_CALL),
        (evidence, "verifier", AuditEventType.TOOL_CALL),
        (execution, "executor", AuditEventType.EXECUTION),
    ):
        if (
            record.task_id != task.id
            or record.collected_at > evidence.collected_at
            or not any(
                a.evidence_id == record.id
                and a.event_type is event_type
                and a.actor == actor
                and a.outcome == "succeeded"
                for a in audits
            )
        ):
            raise PermissionError("发布独立验证缺少真实成功查询或执行审计")
    return report


class ReleaseVerifier(ReleaseStore):
    async def observe(self, request: ReleaseObserveRequest) -> ReleaseObserveResult:
        key = json_object(asdict(request))
        async with self.database.session() as session, session.begin():
            ledger = LedgerService(session)
            previous = await cached(session, UUID(request.task.task_id), "release.observed", key)
            if previous:
                assert isinstance(previous.result_snapshot, dict)
                report = ReleaseVerification.model_validate_json(
                    json.dumps(previous.result_snapshot)
                )
                reports = [
                    e
                    for e in await ledger.evidence_for_task(UUID(request.task.task_id))
                    if e.source_tool == "verify_release"
                    and e.result_snapshot == report.model_dump(mode="json")
                ]
                if len(reports) != 1:
                    raise ValueError("发布观测检查点缺少唯一验证报告")
                return ReleaseObserveResult(
                    TaskSnapshot(
                        request.task.task_id,
                        TaskStatus.RESOLVED
                        if request.final and report.passed
                        else TaskStatus.INVESTIGATING
                        if request.final
                        else TaskStatus.VERIFYING,
                        request.task.version + int(request.final),
                    ),
                    str(reports[0].id),
                    report.passed,
                    report.model_dump_json(),
                )
            task = await lock_task(session, request.task)
            if task.status is not TaskStatus.VERIFYING:
                raise ValueError("发布观测必须处于 VERIFYING")
            await require_automation_active(session, task.id)
            plan_record = await ledger.get_evidence(UUID(request.plan_evidence_id))
            ActionPlan.model_validate_json(json.dumps(plan_record.result_snapshot))
            executions = [
                e
                for e in await ledger.evidence_for_task(task.id)
                if e.source_tool == "execute_action"
                and e.parameters.get("plan_evidence_id") == str(plan_record.id)
            ]
            if plan_record.task_id != task.id or len(executions) != 1:
                raise ValueError("发布观测缺少当前计划的唯一执行回执")
            receipt = ExecutionReceipt.model_validate_json(
                json.dumps(executions[0].result_snapshot)
            )

            async def inspect(_: ReleaseVerifyInput) -> ReleaseVerification:
                evidence = await self.read(
                    session,
                    task.id,
                    "query_release_observation",
                    {"release_id": request.release_id, "start": request.start, "end": request.end},
                    "verifier",
                )
                observation = ReleaseObservation.model_validate_json(
                    json.dumps(evidence.result_snapshot)
                )
                target = observation.target
                expected = receipt.target
                matches = (
                    target.cluster_name,
                    target.deployment_name,
                    target.container_name,
                    target.image,
                    target.replicas,
                    target.uid,
                    target.namespace,
                    target.paused,
                    target.traffic_percent,
                ) == (
                    expected.cluster_name,
                    expected.deployment_name,
                    expected.container_name,
                    expected.image,
                    expected.replicas,
                    expected.uid,
                    expected.namespace,
                    expected.paused,
                    expected.traffic_percent,
                )
                return ReleaseVerification(
                    task_id=task.id,
                    verifying_version=task.status_version,
                    plan_evidence_id=plan_record.id,
                    observation_evidence_id=evidence.id,
                    execution_evidence_id=executions[0].id,
                    final=request.final,
                    target_matches=matches,
                    checks=recovery_checks(observation, self.settings),
                    config_hash=configuration_hash(self.settings),
                )

            registry = ToolRegistry()
            registry.register(
                name="verify_release",
                description="独立校验发布实际回执目标与八项恢复条件",
                input_model=ReleaseVerifyInput,
                output_model=ReleaseVerification,
                handler=inspect,
                risk_level=RiskLevel.L0,
            )
            params = json_object(
                {
                    "task_id": str(task.id),
                    "version": task.status_version,
                    "plan_evidence_id": request.plan_evidence_id,
                    "release_id": request.release_id,
                    "start": request.start,
                    "end": request.end,
                    "final": request.final,
                }
            )
            result = await ToolDispatcher(
                registry, create_policy_engine(self.settings), ledger
            ).dispatch(
                task_id=task.id, tool_name="verify_release", parameters=params, actor="verifier"
            )
            if result.status is not DispatchStatus.SUCCEEDED or result.evidence_id is None:
                raise PermissionError("发布独立验证被 Policy 拒绝")
            report = ReleaseVerification.model_validate_json(json.dumps(result.result))
            await validate_release_report(
                session, task, await ledger.get_evidence(result.evidence_id), require_passed=False
            )
            updated = task
            if request.final:
                with _verification_scope(task.id, task.status_version, result.evidence_id):
                    updated = await TaskService(session).transition(
                        task.id,
                        TaskStatus.RESOLVED if report.passed else TaskStatus.INVESTIGATING,
                        expected_status=task.status,
                        expected_version=task.status_version,
                        reason="发布独立验证通过"
                        if report.passed
                        else "发布独立验证未恢复，重新调查",
                        actor=TransitionActor.VERIFIER,
                    )
            await ledger.append_evidence(
                task_id=task.id,
                source_tool="release.observed",
                parameters=key,
                result_snapshot=json_object(report.model_dump(mode="json")),
            )
            return ReleaseObserveResult(
                TaskSnapshot(str(updated.id), updated.status, updated.status_version),
                str(result.evidence_id),
                report.passed,
                report.model_dump_json(),
            )
