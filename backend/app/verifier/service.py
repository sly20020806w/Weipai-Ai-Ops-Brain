"""任务行锁下验证、留证、迁移原子提交；重试使用已提交报告，不重新采样。"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.executor.verification import require_execution_target
from app.ledger.service import LedgerService
from app.runbooks.lifecycle import RunbookLifecycle
from app.runbooks.maturity import MaturityConfig
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.safety.models import SafetyConfig
from app.tasks.safety.service import SafetyService, abort_record
from app.tasks.service import TaskNotFound, TaskService, TaskStateConflict
from app.tasks.states import TaskStatus, TransitionActor
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus
from app.tools.registry import json_object
from app.triggers.models import OpsEvent
from app.verifier.authority import _verification_scope
from app.verifier.models import (
    VerificationConfig,
    VerificationReport,
    VerificationResult,
    VerificationSpec,
    criteria_hash,
)


class VerificationService:
    def __init__(
        self,
        session: AsyncSession,
        dispatcher: ToolDispatcher,
        config: VerificationConfig,
        safety_config: SafetyConfig | None = None,
        maturity_config: MaturityConfig | None = None,
    ) -> None:
        self.session, self.dispatcher, self.config = session, dispatcher, config
        self.safety_config = safety_config or SafetyConfig()
        self.maturity_config = maturity_config or MaturityConfig()

    async def verify(
        self, snapshot: TaskSnapshot, spec: VerificationSpec, *, reason: str = "独立验证完成"
    ) -> VerificationResult:
        if not self.session.in_transaction():
            raise RuntimeError("验证服务需要外层事务")
        spec = VerificationSpec.model_validate(spec)
        if snapshot.status is not TaskStatus.VERIFYING or (snapshot.task_id, snapshot.version) != (
            str(spec.task_id),
            spec.verifying_version,
        ):
            raise TaskStateConflict("验证目标必须绑定当前 VERIFYING 任务和版本")
        # savepoint 包含全部查询证据、聚合报告、状态历史与审计。
        async with self.session.begin_nested():
            task = await self.session.scalar(
                select(AITask)
                .where(AITask.id == spec.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task is None:
                raise TaskNotFound("验证任务不存在")
            event = await self.session.scalar(select(OpsEvent).where(OpsEvent.task_id == task.id))
            if event is not None and event.service_name != spec.service_name:
                raise TaskStateConflict("验证服务与任务原事件不一致")
            ledger = LedgerService(self.session)
            await require_execution_target(self.session, spec)
            parameters = json_object(spec.model_dump(mode="json"))
            previous = await self.session.scalar(
                select(TaskStatusHistory).where(
                    TaskStatusHistory.task_id == spec.task_id,
                    TaskStatusHistory.sequence == snapshot.version + 1,
                )
            )
            evidence = next(
                (
                    e
                    for e in await ledger.evidence_for_task(spec.task_id)
                    if e.source_tool == "verify_action"
                    and e.parameters == parameters
                    and previous is not None
                    and (
                        previous.reason == f"{reason}；验证证据 {e.id}"
                        or previous.to_status is TaskStatus.AUTOMATION_ABORTED
                    )
                ),
                None,
            )
            if evidence is not None:
                report = VerificationReport.model_validate_json(
                    json.dumps(evidence.result_snapshot)
                )
                target = TaskStatus.RESOLVED if report.passed else TaskStatus.INVESTIGATING
                aborted = await abort_record(self.session, task.id)
                if (
                    previous is not None
                    and previous.to_status is TaskStatus.AUTOMATION_ABORTED
                    and aborted is not None
                    and aborted.parameters.get("phase_version") == snapshot.version
                ):
                    if report.criteria_hash != criteria_hash(self.config):
                        raise TaskStateConflict("验证重投规则变化")
                    return VerificationResult(
                        TaskSnapshot(
                            snapshot.task_id, TaskStatus.AUTOMATION_ABORTED, snapshot.version + 1
                        ),
                        str(evidence.id),
                        report.model_dump_json(),
                    )
                if (
                    report.criteria_hash != criteria_hash(self.config)
                    or previous is None
                    or previous.from_status is not TaskStatus.VERIFYING
                    or previous.to_status is not target
                    or previous.actor is not TransitionActor.VERIFIER
                    or previous.reason != f"{reason}；验证证据 {evidence.id}"
                ):
                    raise TaskStateConflict("验证重试与原已提交请求不一致")
                return VerificationResult(
                    TaskSnapshot(snapshot.task_id, target, snapshot.version + 1),
                    str(evidence.id),
                    report.model_dump_json(),
                )
            if (task.status, task.status_version) != (snapshot.status, snapshot.version):
                raise TaskStateConflict("验证任务状态或版本已变化")
            result = await self.dispatcher.dispatch(
                task_id=spec.task_id,
                tool_name="verify_action",
                parameters=parameters,
                actor="verifier",
                allowed_tools=frozenset({"verify_action"}),
            )
            if result.status is not DispatchStatus.SUCCEEDED or result.result is None:
                raise ValueError("独立验证 Tool 被 Policy 拒绝或执行失败")
            assert result.evidence_id is not None
            report = VerificationReport.model_validate_json(json.dumps(result.result))
            with _verification_scope(spec.task_id, snapshot.version, result.evidence_id):
                await RunbookLifecycle(self.session, self.maturity_config).record_verification(
                    task, result.evidence_id
                )
            guard = await SafetyService(self.session, self.safety_config).check(task)
            if guard.evidence_id:
                return VerificationResult(
                    guard.task, str(result.evidence_id), report.model_dump_json()
                )
            target = TaskStatus.RESOLVED if report.passed else TaskStatus.INVESTIGATING
            with _verification_scope(spec.task_id, snapshot.version, result.evidence_id):
                updated = await TaskService(self.session).transition(
                    spec.task_id,
                    target,
                    expected_status=snapshot.status,
                    expected_version=snapshot.version,
                    actor=TransitionActor.VERIFIER,
                    reason=f"{reason}；验证证据 {result.evidence_id}",
                )
            return VerificationResult(
                TaskSnapshot(str(updated.id), updated.status, updated.status_version),
                str(result.evidence_id),
                report.model_dump_json(),
            )
