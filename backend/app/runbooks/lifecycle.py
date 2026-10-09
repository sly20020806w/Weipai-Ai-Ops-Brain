"""人工审核和独立验证推动成熟度；行锁、只追加证据和审计保证并发幂等。"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.models import RunbookPolicyContext
from app.runbooks.maturity import (
    MaturityConfig,
    MaturityState,
    automation_level,
    config_hash,
    content_hash,
    record_result,
)
from app.runbooks.models import Runbook
from app.runbooks.schemas import RunbookMaturity, RunbookView
from app.runbooks.service import RunbookNotFound, view
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.states import TaskSource, TaskStatus
from app.tools.registry import json_object


class RunbookLifecycle:
    def __init__(self, session: AsyncSession, config: MaturityConfig | None = None) -> None:
        self.session = session
        self.config = config or MaturityConfig()

    async def lock(self, runbook_id: UUID) -> Runbook:
        if not self.session.in_transaction():
            raise RuntimeError("Runbook 成熟度服务需要外层事务")
        entry = await self.session.scalar(
            select(Runbook)
            .where(Runbook.id == runbook_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry is None:
            raise RunbookNotFound("Runbook 不存在")
        return entry

    async def records(self, runbook_id: UUID) -> list[Evidence]:
        return list(
            await self.session.scalars(
                select(Evidence)
                .where(
                    Evidence.source_tool == "runbook.lifecycle",
                    Evidence.parameters["runbook_id"].as_string() == str(runbook_id),
                )
                .order_by(Evidence.collected_at, Evidence.id)
            )
        )

    async def state(self, entry: Runbook) -> tuple[MaturityState, Evidence | None]:
        revision = content_hash(view(entry))
        records = [
            e for e in await self.records(entry.id) if e.parameters.get("revision") == revision
        ]
        if not records:
            return MaturityState(), None
        latest = records[-1]
        state = MaturityState.model_validate_json(json.dumps(latest.result_snapshot))
        audits = await LedgerService(self.session).audits_for_task(latest.task_id)
        if not any(
            a.evidence_id == latest.id
            and a.operation == "runbook.lifecycle"
            and a.outcome == latest.parameters.get("operation")
            for a in audits
        ):
            raise ValueError("成熟度记录缺少宿主审计")
        return state, latest

    async def save(
        self,
        entry: Runbook,
        task_id: UUID,
        state: MaturityState,
        *,
        operation: str,
        actor: str,
        reference: UUID,
        request_id: UUID | None = None,
    ) -> Evidence:
        before = view(entry)
        ledger = LedgerService(self.session)
        params = json_object(
            {
                "runbook_id": str(entry.id),
                "revision": content_hash(before),
                "operation": operation,
                "reference_id": str(reference),
                "request_id": str(request_id) if request_id else None,
                "criteria_hash": config_hash(self.config),
            }
        )
        evidence = await ledger.append_evidence(
            task_id=task_id,
            source_tool="runbook.lifecycle",
            parameters=params,
            result_snapshot=json_object(state.model_dump(mode="json")),
        )
        entry.success_count, entry.failure_count = state.success_count, state.failure_count
        entry.confidence, entry.maturity = state.confidence, state.maturity.value
        entry.automation_level = automation_level(state.maturity).value
        await self.session.flush()
        await ledger.append_audit(
            task_id=task_id,
            event_type=(
                AuditEventType.HUMAN_INTERACTION
                if operation == "review"
                else AuditEventType.EXECUTION
            ),
            actor=actor,
            operation="runbook.lifecycle",
            outcome=operation,
            evidence_id=evidence.id,
            details={
                "runbook_id": str(entry.id),
                "revision": params["revision"],
                "before_maturity": before.maturity.value,
                "after_maturity": entry.maturity,
                "reference_id": str(reference),
            },
        )
        return evidence

    async def review(
        self,
        runbook_id: UUID,
        *,
        task_id: UUID,
        request_id: UUID,
        expected_revision: str,
        actor: str,
        approved: bool,
    ) -> RunbookView:
        """内部人工入口；不得注册为 Agent Tool，操作人由后续鉴权适配层传入。"""
        actor = actor.strip()
        if not actor or len(actor) > 200 or type(approved) is not bool:
            raise ValueError("人工审核需要明确决定与有效操作人")
        task = await self.session.scalar(
            select(AITask).where(AITask.id == task_id).with_for_update()
        )
        if task is None or task.source is not TaskSource.HUMAN:
            raise ValueError("人工审核必须由 Human 任务承载")
        async with self.session.begin_nested():
            entry = await self.lock(runbook_id)
            if content_hash(view(entry)) != expected_revision:
                raise ValueError("Runbook 内容版本已变化，旧审核失效")
            ledger = LedgerService(self.session)
            records = await self.records(runbook_id)
            previous = next(
                (e for e in records if e.parameters.get("request_id") == str(request_id)), None
            )
            request = {"actor": actor, "approved": approved, "revision": expected_revision}
            if previous is not None:
                decision = await ledger.get_evidence(UUID(str(previous.parameters["reference_id"])))
                if decision.task_id != task_id or decision.result_snapshot != request:
                    raise ValueError("重复审核请求内容冲突")
                return view(entry)
            state, _ = await self.state(entry)
            decision = await ledger.append_evidence(
                task_id=task_id,
                source_tool="runbook.review",
                parameters={"runbook_id": str(runbook_id), "request_id": str(request_id)},
                result_snapshot=json_object(request),
            )
            # 审核只提升 Draft 到 Reviewed；不能用一次审核跨越验证阈值。
            updated = state.model_copy(
                update={
                    "maturity": (
                        RunbookMaturity.REVIEWED
                        if approved and state.maturity is RunbookMaturity.DRAFT
                        else state.maturity
                        if approved
                        else RunbookMaturity.DRAFT
                    ),
                    "review_evidence_id": decision.id if approved else None,
                    "consecutive_failures": 0 if approved else state.consecutive_failures,
                }
            )
            await self.save(
                entry,
                task_id,
                updated,
                operation="review",
                actor=actor,
                reference=decision.id,
                request_id=request_id,
            )
            return view(entry)

    async def record_verification(self, task: AITask, verification_id: UUID) -> None:
        """只能在独立 Verifier 的当前调用范围内、迁移之前记账。"""
        from app.verifier.authority import require_verification_outcome

        report = await require_verification_outcome(self.session, task, verification_id)
        if report is None:
            return  # 缺数据、Policy 拒绝不代表 Runbook 失败。
        guide = await selected_runbook(self.session, task.id)
        if guide is None:
            return
        try:
            entry = await self.lock(guide.id)
        except RunbookNotFound:
            return  # 删除不撤销历史验证结果，也不能为不存在的 Runbook 累计。
        if content_hash(view(entry)) != content_hash(guide):
            return  # 历史快照照常验证，但不能影响改过的新 Runbook。
        previous = await self.records(entry.id)
        if any(
            e.task_id == task.id
            and e.parameters.get("operation") == "outcome"
            and e.parameters.get("revision") == content_hash(guide)
            for e in previous
        ):
            return  # 同任务同版本只算一次，Activity 重投/再次观测不会刷次数。
        state, _ = await self.state(entry)
        state = record_result(state, report.passed, self.config)
        await self.save(
            entry, task.id, state, operation="outcome", actor="verifier", reference=verification_id
        )

    async def record_diagnostic_failure(self, task: AITask, observation_id: UUID) -> None:
        """真实 L0 诊断失败可降级；拒绝、模型错误和自主调查失败不计入。"""
        if task.status is not TaskStatus.INVESTIGATING:
            raise ValueError("诊断失败只能绑定当前调查阶段")
        guide = await selected_runbook(self.session, task.id)
        if guide is None:
            return
        ledger = LedgerService(self.session)
        observation = await ledger.get_evidence(observation_id)
        if (
            observation.task_id != task.id
            or observation.source_tool != "agent.observe"
            or observation.parameters.get("phase_version") != task.status_version
        ):
            raise ValueError("Runbook 失败观察不是当前任务和阶段")
        from app.tools.models import DispatchResult, DispatchStatus

        result = DispatchResult.model_validate_json(json.dumps(observation.result_snapshot))
        audits = await ledger.audits_for_task(task.id)
        if result.status is not DispatchStatus.FAILED or not any(
            a.id == result.audit_id
            and a.outcome == "failed"
            and a.actor == "codex-main-agent"
            and a.details.get("mode") == "live"
            and a.operation in {step.tool_name for step in guide.diagnostic_steps}
            for a in audits
        ):
            return
        try:
            entry = await self.lock(guide.id)
        except RunbookNotFound:
            return
        if content_hash(view(entry)) != content_hash(guide):
            return
        if any(
            e.task_id == task.id
            and e.parameters.get("operation") == "outcome"
            and e.parameters.get("revision") == content_hash(guide)
            for e in await self.records(entry.id)
        ):
            return
        state, _ = await self.state(entry)
        await self.save(
            entry,
            task.id,
            record_result(state, False, self.config),
            operation="outcome",
            actor="codex-main-agent",
            reference=observation_id,
        )

    async def context(self, guide: RunbookView, *, lock: bool = True) -> RunbookPolicyContext:
        entry = (
            await self.lock(guide.id)
            if lock
            else await self.session.get(Runbook, guide.id, populate_existing=True)
        )
        if entry is None:
            raise RunbookNotFound("Runbook 不存在")
        if content_hash(view(entry)) != content_hash(guide):
            raise ValueError("选中的 Runbook 内容已变化，需要重新调查")
        state, record = await self.state(entry)
        current = view(entry)
        trusted = (
            record is not None
            and state.review_evidence_id is not None
            and record.parameters.get("criteria_hash") == config_hash(self.config)
            and (state.success_count, state.failure_count, state.confidence, state.maturity)
            == (current.success_count, current.failure_count, current.confidence, current.maturity)
            and state.success_count >= self.config.self_healing_successes
            and state.confidence >= self.config.self_healing_confidence
            and state.consecutive_failures == 0
        )
        return RunbookPolicyContext(
            runbook_id=entry.id,
            revision=content_hash(current),
            maturity=state.maturity.value,
            trusted=trusted,
            review_evidence_id=state.review_evidence_id,
            state_evidence_id=record.id if record else None,
        )


async def task_runbook_context(
    session: AsyncSession,
    task_id: UUID,
    config: MaturityConfig,
    *,
    lock: bool = True,
) -> RunbookPolicyContext | None:
    guide = await selected_runbook(session, task_id)
    if guide is None:
        return None
    return await RunbookLifecycle(session, config).context(guide, lock=lock)


async def selected_runbook(session: AsyncSession, task_id: UUID) -> RunbookView | None:
    ledger = LedgerService(session)
    records = await ledger.evidence_for_task(task_id)
    matches = [e for e in records if e.source_tool == "runbook.match"]
    if not matches:
        return None
    matched = matches[-1]
    snapshot = matched.result_snapshot
    if not isinstance(snapshot, dict) or not snapshot.get("runbook_json"):
        return None
    guide = RunbookView.model_validate_json(str(snapshot["runbook_json"]))
    investigation = await session.scalar(
        select(TaskStatusHistory)
        .where(
            TaskStatusHistory.task_id == task_id,
            TaskStatusHistory.to_status == TaskStatus.INVESTIGATING,
        )
        .order_by(TaskStatusHistory.sequence.desc())
        .limit(1)
    )
    if investigation is None:
        return None
    phase = matched.parameters.get("phase_version")
    if investigation.sequence != int(str(phase)) + 1:
        answered = False
        for record in records:
            prompt = record.parameters.get("prompt")
            if record.source_tool != "human.answer" or not isinstance(prompt, dict):
                continue
            waiting_task = prompt.get("task")
            if (
                prompt.get("resume_status") == TaskStatus.INVESTIGATING.value
                and isinstance(waiting_task, dict)
                and waiting_task.get("version") == investigation.sequence - 1
            ):
                answered = True
        if not answered or investigation.sequence != int(str(phase)) + 3:
            return None  # 反证或验证失败后转自主调查，旧 Runbook 不再影响结果和 Policy。
    search_id = UUID(str(snapshot.get("search_evidence_id")))
    search = await ledger.get_evidence(search_id)
    audits = await ledger.audits_for_task(task_id)
    if (
        search.task_id != task_id
        or search.source_tool != "search_runbooks"
        or not any(
            a.evidence_id == search_id
            and a.operation == "search_runbooks"
            and a.outcome == "succeeded"
            and a.details.get("mode") == "live"
            for a in audits
        )
    ):
        raise ValueError("Runbook 匹配缺少本任务真实检索证据")
    from app.tools.runbooks import SearchRunbooksOutput

    hits = SearchRunbooksOutput.model_validate_json(json.dumps(search.result_snapshot))
    if not any(hit.runbook == guide for hit in hits.matches):
        raise ValueError("Runbook 匹配快照不属于原检索结果")
    return guide
