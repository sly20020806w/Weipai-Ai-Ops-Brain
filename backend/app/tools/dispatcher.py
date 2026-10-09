"""唯一调用入口：定级、Policy、执行/回放、证据和审计。无调度与自动重试。"""

from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from app.db.base import UTCDateTime, utc_now
from app.executor.authority import execution_active, execution_authority
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import EvidenceNotFound, LedgerService
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyAction, PolicyDecision, PolicyResult, RiskLevel
from app.tasks.models import AITask
from app.tasks.service import TaskNotFound
from app.tools.models import DispatchMode, DispatchResult, DispatchStatus, JsonObject
from app.tools.registry import ToolNotFound, ToolRegistry, _RegisteredTool, json_object


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine, ledger: LedgerService) -> None:
        self._registry = registry
        self._policy = policy
        self._ledger = ledger

    async def dispatch(
        self,
        *,
        task_id: UUID,
        tool_name: str,
        parameters: JsonObject,
        actor: str,
        mode: DispatchMode = DispatchMode.LIVE,
        replay_evidence_id: UUID | None = None,
        replay_before: datetime | None = None,
        allowed_tools: frozenset[str] | None = None,
    ) -> DispatchResult:
        session = self._ledger.session
        if not session.in_transaction():
            raise RuntimeError("调用 Dispatcher 前请使用 async with session.begin() 开启事务")
        if not isinstance(mode, DispatchMode):
            raise TypeError("调用模式必须使用 DispatchMode")
        actor = actor.strip()
        if not actor or len(actor) > 200:
            raise ValueError("actor 必须非空且不超过 200 字符")
        # 身份与模式来自宿主服务；Agent 不能传入风险、环境或 approved 布尔值。
        action = PolicyAction(name=tool_name)
        if len(tool_name) > 64:
            raise ValueError("Tool 名称不能超过 64 字符")
        if await session.get(AITask, task_id) is None:
            raise TaskNotFound(f"任务不存在：{task_id}")
        try:
            tool = self._registry._get(tool_name)
        except ToolNotFound:
            return await self._record(
                task_id, actor, mode, self._policy.evaluate(action), error_code="tool_not_found"
            )
        policy = self._policy.evaluate(
            PolicyAction(name=tool_name, risk_level=tool.declaration.risk_level)
        )
        if mode is DispatchMode.LIVE and (
            policy.risk_level is not RiskLevel.L0 or tool_name == "execute_action"
        ):
            # 锁存记录永久阻断写动作，包括旧审批、恢复状态与直接 Dispatcher 调用。
            # Replay 仍读取历史快照，绝不签发凭证。
            if any(
                evidence.source_tool == "safety.abort"
                for evidence in await self._ledger.evidence_for_task(task_id)
            ):
                return await self._record(
                    task_id, actor, mode, policy, error_code="automation_aborted"
                )
        authority = (
            execution_authority(task_id, parameters, id(session))
            if tool_name == "execute_action"
            else None
        )
        if authority is not None:
            current_policy = self._policy.evaluate(
                PolicyAction(
                    name=authority.policy.action_name,
                    risk_level=authority.policy.risk_level,
                    runbook=authority.runbook,
                )
            )
            if current_policy != authority.policy:
                return await self._record(
                    task_id, actor, mode, current_policy, error_code="execution_policy_changed"
                )
            policy = current_policy
        if allowed_tools is not None and tool_name not in allowed_tools:
            return await self._record(
                task_id, actor, mode, policy, error_code="expert_tool_not_allowed"
            )
        if policy.decision is PolicyDecision.DENY or (
            policy.decision is PolicyDecision.NEED_APPROVAL and authority is None
        ):
            code = (
                "approval_required"
                if policy.decision is PolicyDecision.NEED_APPROVAL
                else "policy_denied"
            )
            return await self._record(task_id, actor, mode, policy, error_code=code)
        if (
            mode is DispatchMode.LIVE
            and (policy.risk_level is not RiskLevel.L0 or tool_name == "execute_action")
            and authority is None
        ):
            # 写入仅对 Executor 已验收的精确计划开放，其他 Tool 保持原门禁。
            return await self._record(
                task_id, actor, mode, policy, error_code="write_execution_not_ready"
            )
        try:
            model, normalized = tool.prepare(parameters)
        except (ValidationError, ValueError, TypeError):
            return await self._record(task_id, actor, mode, policy, error_code="invalid_parameters")
        if mode is DispatchMode.REPLAY:
            return await self._replay(
                task_id, actor, policy, tool, normalized, replay_evidence_id, replay_before
            )
        if replay_evidence_id is not None or replay_before is not None:
            return await self._record(task_id, actor, mode, policy, error_code="invalid_replay")
        try:
            result = tool.validate_result(await tool.invoke(model))
        except Exception:
            # 错误内容可能带源系统凭证；仅留下固定错误码，不存异常文本。
            return await self._record(
                task_id, actor, mode, policy, error_code="tool_failed", failed=True
            )
        # savepoint 保证证据与审计成对写入；外层事务仍由调用方提交。
        async with session.begin_nested():
            evidence = await self._ledger.append_evidence(
                task_id=task_id,
                source_tool=tool_name,
                parameters=normalized,
                result_snapshot=result,
                collected_at=utc_now(),
            )
            return await self._record(
                task_id, actor, mode, policy, result=result, evidence=evidence
            )

    async def _replay(
        self,
        task_id: UUID,
        actor: str,
        policy: PolicyResult,
        tool: _RegisteredTool,
        parameters: JsonObject,
        evidence_id: UUID | None,
        before: datetime | None,
    ) -> DispatchResult:
        mode = DispatchMode.REPLAY
        try:
            cutoff = UTCDateTime.normalize(before)
        except (ValueError, TypeError):
            cutoff = None
        if evidence_id is None or cutoff is None:
            return await self._record(task_id, actor, mode, policy, error_code="invalid_replay")
        try:
            evidence = await self._ledger.get_evidence(evidence_id)
        except EvidenceNotFound:
            return await self._record(
                task_id, actor, mode, policy, error_code="replay_evidence_not_found"
            )
        if (
            evidence.task_id != task_id
            or evidence.source_tool != tool.declaration.name
            or evidence.parameters != parameters
            or evidence.collected_at > cutoff
            or (evidence.created_at is not None and evidence.created_at > cutoff)
        ):
            return await self._record(task_id, actor, mode, policy, error_code="replay_mismatch")
        # 只回放有成功调用审计且包含可校验快照的历史结果；绝不联网补查。
        audits = await self._ledger.audits_for_task(task_id)
        if not any(
            audit.event_type is AuditEventType.TOOL_CALL
            and audit.operation == tool.declaration.name
            and audit.evidence_id == evidence_id
            and audit.outcome == DispatchStatus.SUCCEEDED.value
            and audit.occurred_at <= cutoff
            and (audit.created_at is None or audit.created_at <= cutoff)
            and audit.details.get("mode") == DispatchMode.LIVE.value
            for audit in audits
        ):
            return await self._record(
                task_id, actor, mode, policy, error_code="replay_missing_success_audit"
            )
        try:
            result = tool.validate_snapshot(evidence.result_snapshot)
        except (ValidationError, ValueError, TypeError):
            return await self._record(
                task_id, actor, mode, policy, error_code="replay_invalid_snapshot"
            )
        return await self._record(task_id, actor, mode, policy, result=result, evidence=evidence)

    async def _record(
        self,
        task_id: UUID,
        actor: str,
        mode: DispatchMode,
        policy: PolicyResult,
        *,
        result: JsonObject | None = None,
        evidence: Evidence | None = None,
        error_code: str | None = None,
        failed: bool = False,
    ) -> DispatchResult:
        status = (
            DispatchStatus.FAILED
            if failed
            else DispatchStatus.REJECTED
            if error_code is not None
            else DispatchStatus.REPLAYED
            if mode is DispatchMode.REPLAY
            else DispatchStatus.SUCCEEDED
        )
        audit = await self._ledger.append_audit(
            task_id=task_id,
            event_type=AuditEventType.TOOL_CALL,
            actor=actor,
            operation=(
                "execute_action"
                if execution_active(task_id, id(self._ledger.session))
                else policy.action_name
            ),
            outcome=status.value,
            details={
                "mode": mode.value,
                "policy": json_object(policy.model_dump(mode="json")),
                "error_code": error_code,
            },
            evidence_id=None if evidence is None else evidence.id,
        )
        return DispatchResult(
            status=status,
            policy=policy,
            audit_id=audit.id,
            evidence_id=None if evidence is None else evidence.id,
            result=None if result is None else json_object(result),
            error_code=error_code,
        )
