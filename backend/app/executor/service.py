"""先提交执行意图，再调用幂等动作端；审计失败和丢响应按同一命令重试。

执行端必须持久化命令幂等键；不能把外部副作用假装成数据库原子事务。
本模块只执行既定计划，状态写入仍由 tasks 服务完成。
"""

import json
from dataclasses import asdict
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.kubernetes.execution import (
    ExecutionReceipt,
    KubernetesWriteConnector,
    fake_binding,
)
from app.db.base import utc_now
from app.db.session import Database
from app.executor.authority import _execution_scope, _ExecutionAuthority
from app.executor.models import (
    ActionName,
    ExecutionBinding,
    ExecutionCommand,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTarget,
    command_hash,
)
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyDecision, RiskLevel
from app.tasks.approval.models import action_hash
from app.tasks.approval.service import ApprovalStore, check_policy, check_runbook
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan, PlannedAction, RollbackParameters
from app.tasks.review_gate import require_review_for_planning
from app.tasks.safety.models import AutomationAborted
from app.tasks.safety.service import SafetyService, SafetyStore, require_automation_active
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.execution import register_execution_tools
from app.tools.models import DispatchMode, DispatchStatus, JsonObject
from app.tools.registry import ToolRegistry, json_object


def binding_for(settings: Settings, service_name: str) -> ExecutionBinding:
    bindings = settings.execution_config.bindings
    if (
        not bindings
        and settings.app_env in {"local", "test"}
        and settings.connector_mode.value == "fake"
    ):
        bindings = (fake_binding(),)
    matches = [b for b in bindings if b.service_name == service_name]
    if len(matches) != 1:
        raise ValueError("执行服务没有唯一宿主绑定")
    return ExecutionBinding.model_validate(matches[0])


def validate_action(action: PlannedAction, binding: ExecutionBinding) -> ActionName:
    if action.risk_level not in {RiskLevel.L3, RiskLevel.L4, RiskLevel.L5}:
        raise ValueError("首批动作风险不能低于 L3")
    if action.name == "rollback_prod":
        params = RollbackParameters.model_validate(action.parameters)
        if params.from_version not in binding.images or params.to_version not in binding.images:
            raise ValueError("回滚版本未在宿主白名单中验证")
        return "rollback_prod"
    if action.name == "deploy_service":
        from app.tasks.releases.models import DeployParameters

        deploy_params = DeployParameters.model_validate(action.parameters)
        if (
            deploy_params.from_version not in binding.images
            or deploy_params.to_version not in binding.images
        ):
            raise ValueError("发布版本未在宿主白名单中验证")
        return "deploy_service"
    if action.name == "pause_release":
        if action.parameters != {"paused": True}:
            raise ValueError("暂停必须明确停止当前发布")
        return "pause_release"
    if action.name == "restart_service":
        if action.parameters != {"strategy": "rolling"}:
            raise ValueError("只允许精确的滚动重启参数")
        return "restart_service"
    if action.name == "scale_service":
        if set(action.parameters) != {"from_replicas", "to_replicas"}:
            raise ValueError("扩缩容需要精确的原始和目标副本数")
        values = (action.parameters["from_replicas"], action.parameters["to_replicas"])
        if any(
            type(v) is not int or not binding.min_replicas <= v <= binding.max_replicas
            for v in values
        ):
            raise ValueError("扩缩容超出最小权限副本范围")
        if values[0] == values[1]:
            raise ValueError("扩缩容必须改变副本数")
        return "scale_service"
    raise ValueError("动作不在首批执行白名单中")


def make_command(
    plan: ActionPlan,
    plan_id: UUID,
    action: PlannedAction,
    binding: ExecutionBinding,
    target: ExecutionTarget,
) -> ExecutionCommand:
    name = validate_action(action, binding)
    if any(
        getattr(target, f) != getattr(binding, f)
        for f in ("service_name", "cluster_name", "namespace", "deployment_name", "container_name")
    ):
        raise ValueError("读回的执行资源不属于批准服务")
    image, replicas = target.image, target.replicas
    release_fields: dict[str, object] = {}
    if name == "deploy_service":
        from app.tasks.releases.models import DeployParameters

        deploy = DeployParameters.model_validate(action.parameters)
        if target.image != binding.images[deploy.from_version] or target.paused:
            raise ValueError("发布前版本或暂停状态与授权前提不符")
        image = binding.images[deploy.to_version]
        release_fields = {
            "expected_paused": False,
            "expected_traffic_percent": deploy.traffic_percent,
        }
    if name == "pause_release":
        if target.paused:
            raise ValueError("发布已经暂停，须核对原执行意图")
        release_fields = {
            "expected_paused": True,
            "expected_traffic_percent": target.traffic_percent,
        }
    if name == "rollback_prod":
        params = RollbackParameters.model_validate(action.parameters)
        if target.image != binding.images[params.from_version]:
            raise ValueError("当前镜像与已批准回滚前提不符")
        image = binding.images[params.to_version]
        if target.paused or target.traffic_percent != 100:
            release_fields = {"expected_paused": False, "expected_traffic_percent": 100}
    if name == "scale_service":
        if target.replicas != action.parameters["from_replicas"]:
            raise ValueError("当前副本数与已批准扩缩容前提不符")
        replicas = int(action.parameters["to_replicas"])  # type: ignore[arg-type]
    return ExecutionCommand(
        execution_id=uuid5(NAMESPACE_URL, f"{plan_id}/{action.id}/{action_hash(plan)}"),
        task_id=plan.task_id,
        plan_evidence_id=plan_id,
        plan_hash=action_hash(plan),
        action_id=action.id,
        name=name,
        target=target,
        expected_image=image,
        expected_replicas=replicas,
        **release_fields,  # type: ignore[arg-type]
    )


class ExecutionStore:
    def __init__(
        self, database: Database, settings: Settings, connector: KubernetesWriteConnector
    ) -> None:
        self.database, self.settings, self.connector = (
            database,
            Settings.model_validate(settings),
            connector,
        )

    def dispatcher(self, session: AsyncSession) -> ToolDispatcher:
        registry = ToolRegistry()
        register_execution_tools(
            registry, self.connector, self.settings.execution_config.credential_ttl_seconds
        )
        return ToolDispatcher(registry, create_policy_engine(self.settings), LedgerService(session))

    async def authorize(
        self, session: AsyncSession, request: ExecutionRequest
    ) -> tuple[AITask, ActionPlan]:
        if not self.settings.execution_config.enabled:
            raise PermissionError("宿主尚未启用 Executor")
        task = await session.scalar(
            select(AITask).where(AITask.id == UUID(request.task.task_id)).with_for_update()
        )
        if task is None:
            raise ValueError("执行任务不存在")
        await require_automation_active(session, task.id)
        record = await LedgerService(session).get_evidence(UUID(request.plan_evidence_id))
        plan = ActionPlan.model_validate_json(json.dumps(record.result_snapshot))
        if (
            record.task_id != task.id
            or record.source_tool != "action_plan"
            or plan.task_id != task.id
            or record.parameters.get("phase_version") != plan.planning_version
            or record.parameters.get("conclusion_evidence_id") != str(plan.conclusion_evidence_id)
            or record.parameters.get("review_evidence_id") != str(plan.review_evidence_id)
        ):
            raise ValueError("执行计划必须来自本任务的真实规划证据")
        check_policy(plan, self.settings)
        await check_runbook(session, plan, self.settings)
        await require_review_for_planning(session, task)
        if any(
            e.source_tool == "war_room.submission"
            for e in await LedgerService(session).evidence_for_task(task.id)
        ):
            from app.tasks.war_room.service import require_war_room_plan

            await require_war_room_plan(session, task, plan, self.settings)
        if any(
            item.action.name in {"deploy_service", "pause_release"} for item in plan.actions
        ) or any(
            e.source_tool == "release.assessment"
            for e in await LedgerService(session).evidence_for_task(task.id)
        ):
            from app.tasks.releases.service import require_release_plan

            await require_release_plan(session, task, plan, self.settings)
        if plan.decision is PolicyDecision.DENY:
            raise PermissionError("Policy 禁止执行")
        expected_version = plan.planning_version + (
            2 if plan.decision is PolicyDecision.NEED_APPROVAL else 1
        )
        if (task.status, task.status_version, request.task.status, request.task.version) != (
            TaskStatus.EXECUTING,
            expected_version,
            TaskStatus.EXECUTING,
            expected_version,
        ):
            raise ValueError("执行必须绑定当前 EXECUTING 版本")
        if plan.decision is PolicyDecision.NEED_APPROVAL:
            if (
                request.approval is None
                or request.approval.plan_evidence_id != request.plan_evidence_id
                or not await ApprovalStore(self.database, self.settings).is_approved(
                    request.approval, plan
                )
            ):
                raise PermissionError("动作缺少有效审批")
        for item in plan.actions:
            validate_action(item.action, binding_for(self.settings, item.action.service_name))
        return task, plan

    async def intent(self, request: ExecutionRequest, action_index: int) -> ExecutionCommand:
        async with self.database.session() as session, session.begin():
            task, plan = await self.authorize(session, request)
            action = plan.actions[action_index].action
            guard = await SafetyService(session, self.settings.safety_config).check(
                task, pending_action=(request.plan_evidence_id, action.id)
            )
            if guard.evidence_id:
                # 必须先提交熔断，不能在事务内部抛错回滚安全状态。
                command = None
            else:
                command = await self._intent(session, task, plan, request, action_index)
        if command is None:
            raise AutomationAborted("执行前熔断，未签发动作凭证")
        return command

    async def _intent(
        self,
        session: AsyncSession,
        task: AITask,
        plan: ActionPlan,
        request: ExecutionRequest,
        action_index: int,
    ) -> ExecutionCommand:
        action = plan.actions[action_index].action
        records = await LedgerService(session).evidence_for_task(task.id)
        cached = next(
            (
                e
                for e in records
                if e.source_tool == "execution.intent"
                and e.parameters.get("plan_evidence_id") == request.plan_evidence_id
                and e.parameters.get("action_id") == action.id
            ),
            None,
        )
        if cached:
            command = ExecutionCommand.model_validate_json(json.dumps(cached.result_snapshot))
            if command.plan_hash != action_hash(plan):
                raise ValueError("已提交执行意图与计划不一致")
            return command
        binding = binding_for(self.settings, action.service_name)
        observed = await self.dispatcher(session).dispatch(
            task_id=task.id,
            tool_name="get_execution_target",
            parameters={
                f: getattr(binding, f)
                for f in (
                    "service_name",
                    "cluster_name",
                    "namespace",
                    "deployment_name",
                    "container_name",
                )
            },
            actor="executor",
        )
        if observed.status is not DispatchStatus.SUCCEEDED:
            raise ValueError("执行前目标读取失败或被 Policy 拒绝")
        target = ExecutionTarget.model_validate_json(json.dumps(observed.result))
        if action.id.startswith("war-room-"):
            from app.tasks.war_room.service import load_assessment

            assessment = await load_assessment(session, task, plan.conclusion_evidence_id)
            if target != assessment.target:
                raise PermissionError("保障资源被其他操作修改，旧检查与审批不能执行")
        command = make_command(plan, UUID(request.plan_evidence_id), action, binding, target)
        ledger = LedgerService(session)
        evidence = await ledger.append_evidence(
            task_id=task.id,
            source_tool="execution.intent",
            parameters={"plan_evidence_id": request.plan_evidence_id, "action_id": action.id},
            result_snapshot=json_object(command.model_dump(mode="json")),
        )
        await ledger.append_audit(
            task_id=task.id,
            event_type=AuditEventType.EXECUTION,
            actor="executor",
            operation="execution.prepare",
            outcome="prepared",
            evidence_id=evidence.id,
            details={
                "command_hash": command_hash(command),
                "target_evidence_id": str(observed.evidence_id),
            },
        )
        return command

    async def execute_one(self, request: ExecutionRequest, command: ExecutionCommand) -> UUID:
        async with self.database.session() as session, session.begin():
            task, plan = await self.authorize(session, request)
            ledger = LedgerService(session)
            params = json_object(command.model_dump(mode="json"))
            intents = [
                e
                for e in await ledger.evidence_for_task(task.id)
                if e.source_tool == "execution.intent" and e.result_snapshot == params
            ]
            if (
                len(intents) != 1
                or command.plan_hash != action_hash(plan)
                or command.plan_evidence_id != UUID(request.plan_evidence_id)
            ):
                raise PermissionError("执行命令必须精确匹配本任务已提交的执行意图")
            cached = next(
                (
                    e
                    for e in await ledger.evidence_for_task(task.id)
                    if e.source_tool == "execute_action" and e.parameters == params
                ),
                None,
            )
            if cached:
                return cached.id
            item = next(i for i in plan.actions if i.action.id == command.action_id)
            reconstructed = make_command(
                plan,
                UUID(request.plan_evidence_id),
                item.action,
                binding_for(self.settings, item.action.service_name),
                command.target,
            )
            if reconstructed != command:
                raise PermissionError("宿主目标绑定变化，已提交执行意图不能改写")
            with _execution_scope(
                _ExecutionAuthority(task.id, params, item.policy, id(session), plan.runbook)
            ):
                result = await self.dispatcher(session).dispatch(
                    task_id=task.id,
                    tool_name="execute_action",
                    parameters=params,
                    actor="executor",
                )
            if result.status is not DispatchStatus.SUCCEEDED or result.evidence_id is None:
                # 提交失败调用审计后再由宿主抛错，保留执行意图便于核对/幂等恢复。
                failure = True
            else:
                receipt = ExecutionReceipt.model_validate_json(json.dumps(result.result))
                expected = command.target.model_copy(
                    update={
                        "image": command.expected_image,
                        "replicas": command.expected_replicas,
                        "resource_version": receipt.target.resource_version,
                        "paused": command.target.paused
                        if command.expected_paused is None
                        else command.expected_paused,
                        "traffic_percent": command.target.traffic_percent
                        if command.expected_traffic_percent is None
                        else command.expected_traffic_percent,
                    }
                )
                if (
                    receipt.execution_id != command.execution_id
                    or receipt.command_hash != command_hash(command)
                    or receipt.target != expected
                ):
                    raise ValueError("动作回执不能证明批准的变更已提交")
                await ledger.append_audit(
                    task_id=task.id,
                    event_type=AuditEventType.EXECUTION,
                    actor="executor",
                    operation=command.name,
                    outcome="succeeded",
                    evidence_id=result.evidence_id,
                    details={
                        "execution_id": str(command.execution_id),
                        "plan_hash": command.plan_hash,
                        "plan_evidence_id": request.plan_evidence_id,
                        "approval_id": request.approval.approval_id if request.approval else None,
                    },
                )
                return result.evidence_id
        if failure:
            guard = await SafetyStore(self.database, self.settings.safety_config).check(
                request.task
            )
            if guard.evidence_id:
                raise AutomationAborted("连续操作失败已熔断，禁止后续重试动作")
            raise RuntimeError("动作端未返回可信成功结果；按原幂等键重试或转人工核对")
        raise RuntimeError("动作结果缺失")

    async def completed(self, request: ExecutionRequest) -> ExecutionResult | None:
        async with self.database.session() as session:
            cached = next(
                (
                    e
                    for e in await LedgerService(session).evidence_for_task(
                        UUID(request.task.task_id)
                    )
                    if e.source_tool == "execution.complete"
                    and e.parameters == json_object(asdict(request))
                ),
                None,
            )
            if cached:
                assert isinstance(cached.result_snapshot, dict)
                ids = cached.result_snapshot["evidence_ids"]
                assert isinstance(ids, list)
                return ExecutionResult(
                    TaskSnapshot(
                        request.task.task_id, TaskStatus.VERIFYING, request.task.version + 1
                    ),
                    [str(x) for x in ids],
                )
        return None

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not self.settings.execution_config.enabled:
            raise PermissionError("宿主尚未启用 Executor")
        try:
            return await self._execute(request)
        except ValueError:
            cached = await self.completed(request)
            if cached:
                return cached
            raise

    async def _execute(self, request: ExecutionRequest) -> ExecutionResult:
        # 已提交后的 Activity 重投不再签发凭证/调用 Connector。
        payload = json_object(asdict(request))
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == UUID(request.task.task_id)).with_for_update()
            )
            if task is None:
                raise ValueError("执行任务不存在")
            cached = next(
                (
                    e
                    for e in await LedgerService(session).evidence_for_task(task.id)
                    if e.source_tool == "execution.complete" and e.parameters == payload
                ),
                None,
            )
            if cached:
                assert isinstance(cached.result_snapshot, dict)
                ids = cached.result_snapshot["evidence_ids"]
                assert isinstance(ids, list) and all(isinstance(x, str) for x in ids)
                return ExecutionResult(
                    TaskSnapshot(str(task.id), TaskStatus.VERIFYING, request.task.version + 1),
                    [str(x) for x in ids],
                )
            try:
                _, plan = await self.authorize(session, request)
                error = None
            except (ValueError, LookupError, PermissionError) as exc:
                await LedgerService(session).append_audit(
                    task_id=task.id,
                    event_type=AuditEventType.EXECUTION,
                    actor="executor",
                    operation="execute_action",
                    outcome="rejected",
                    details={
                        "error_code": type(exc).__name__,
                        "plan_evidence_id": request.plan_evidence_id,
                    },
                )
                error = exc
        if error:
            raise error
        ids = [
            str(await self.execute_one(request, await self.intent(request, index)))
            for index in range(len(plan.actions))
        ]
        async with self.database.session() as session, session.begin():
            task, _ = await self.authorize(session, request)
            ledger = LedgerService(session)
            # 并发调用可能已由另一个宿主完成迁移；在最终事务再次验收。
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="execution.complete",
                parameters=payload,
                result_snapshot={"evidence_ids": ids},
            )
            updated = await TaskService(session).transition(
                task.id,
                TaskStatus.VERIFYING,
                expected_status=request.task.status,
                expected_version=request.task.version,
                reason=f"动作已提交，等待独立验证；执行证据 {evidence.id}",
            )
            return ExecutionResult(
                TaskSnapshot(str(task.id), updated.status, updated.status_version),
                [str(x) for x in ids],
            )

    async def replay(self, request: ExecutionRequest, evidence_id: UUID) -> JsonObject:
        """历史成功执行仅回放原结果，当前 Policy deny 仍拒绝。"""
        async with self.database.session() as session, session.begin():
            ledger = LedgerService(session)
            evidence = await ledger.get_evidence(evidence_id)
            plan_record = await ledger.get_evidence(UUID(request.plan_evidence_id))
            plan = ActionPlan.model_validate_json(json.dumps(plan_record.result_snapshot))
            command = ExecutionCommand.model_validate_json(json.dumps(evidence.parameters))
            if (
                evidence.task_id != plan.task_id
                or str(plan.task_id) != request.task.task_id
                or command.plan_evidence_id != plan_record.id
                or command.plan_hash != action_hash(plan)
            ):
                raise ValueError("回放动作与计划不一致")
            check_policy(plan, self.settings)
            item = next(i for i in plan.actions if i.action.id == command.action_id)
            params = json_object(command.model_dump(mode="json"))
            with _execution_scope(
                _ExecutionAuthority(plan.task_id, params, item.policy, id(session), plan.runbook)
            ):
                result = await self.dispatcher(session).dispatch(
                    task_id=plan.task_id,
                    tool_name="execute_action",
                    parameters=params,
                    actor="executor-replay",
                    mode=DispatchMode.REPLAY,
                    replay_evidence_id=evidence_id,
                    # 这里回读已完成回执；采集瞬间尚未有本次入库与成功审计。
                    replay_before=utc_now(),
                )
            if result.status is not DispatchStatus.REPLAYED or result.result is None:
                raise ValueError("执行回放被拒绝")
            return result.result
