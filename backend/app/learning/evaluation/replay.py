"""主 Agent 重调查历史事故；每一步只调用 Dispatcher REPLAY 并保存可恢复检查点。"""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.activities import configured_llm
from app.agent.client import LLMClient
from app.agent.investigation import AgentConclusion, AgentStepLimit, InvalidConclusion, MainAgent
from app.agent.models import ChatRequest, ChatResponse, ToolCall, ToolDefinition, ToolFunction
from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.learning.evaluation.models import (
    EvaluationLabel,
    ReplayReport,
    ReplayRequest,
    root_cause_hit,
)
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.schemas import RunbookView
from app.runbooks.workflow_models import spec_hash
from app.tasks.models import AITask
from app.tasks.states import TaskStatus
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchResult, DispatchStatus, JsonObject
from app.tools.registry import json_object
from app.tools.replay import replay_registry


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def available(evidence: Evidence, cutoff: datetime) -> bool:
    return evidence.collected_at <= cutoff and evidence.created_at <= cutoff


def successful(audit: AuditRecord, evidence: Evidence, cutoff: datetime) -> bool:
    return (
        audit.event_type is AuditEventType.TOOL_CALL
        and audit.operation == evidence.source_tool
        and audit.evidence_id == evidence.id
        and audit.outcome == "succeeded"
        and audit.details.get("mode") == "live"
        and audit.occurred_at <= cutoff
        and audit.created_at <= cutoff
    )


class ReplayUnavailable(ValueError):
    pass


class ReplayIO:
    def __init__(
        self,
        session: AsyncSession,
        request: ReplayRequest,
        settings: Settings,
        records: list[Evidence],
        audits: list[AuditRecord],
        llm_factory: Callable[[ChatRequest], LLMClient],
    ) -> None:
        self.session, self.request, self.llm_factory = session, request, llm_factory
        self.ledger = LedgerService(session)
        self.registry = replay_registry()
        self.dispatcher = ToolDispatcher(self.registry, create_policy_engine(settings), self.ledger)
        self.records = [
            e
            for e in records
            if available(e, request.cutoff)
            and any(successful(a, e, request.cutoff) for a in audits)
        ]
        self.definitions = tuple(
            ToolDefinition(
                function=ToolFunction(
                    name=d.name, description=d.description, parameters=d.input_schema
                )
            )
            for d in self.registry.declarations()
        )
        self.observed: list[UUID] = []
        self.tool_calls = 0

    async def lock(self) -> None:
        task = await self.session.scalar(
            select(AITask).where(AITask.id == self.request.task_id).with_for_update()
        )
        if task is None or task.status is not TaskStatus.CLOSED:
            raise ReplayUnavailable("Replay 只接受已关闭事故")

    def key(self, step: int, payload: object) -> JsonObject:
        return {
            "run_id": str(self.request.run_id),
            "step": step,
            "run_hash": fingerprint(self.request.model_dump(mode="json")),
            "request_hash": fingerprint(payload),
        }

    async def cached(self, source: str, key: JsonObject) -> Evidence | None:
        record = await self.session.scalar(
            select(Evidence).where(
                Evidence.task_id == self.request.task_id,
                Evidence.source_tool == source,
                Evidence.parameters["run_id"].as_string() == str(self.request.run_id),
                Evidence.parameters["step"].as_integer() == key["step"],
            )
        )
        if record is not None and record.parameters != key:
            raise ReplayUnavailable("同一 Replay 检查点输入冲突")
        return record

    async def think(self, step: int, request: ChatRequest) -> ChatResponse:
        key = self.key(step, request.model_dump(mode="json"))
        async with self.session.begin():
            await self.lock()
            cached = await self.cached("replay.think", key)
            if cached:
                return ChatResponse.model_validate_json(json.dumps(cached.result_snapshot))
            llm = self.llm_factory(request)
            try:
                response = await llm.chat(request)
                response = ChatResponse.model_validate_json(response.model_dump_json())
            finally:
                await llm.aclose()
            await self.ledger.append_evidence(
                task_id=self.request.task_id,
                source_tool="replay.think",
                parameters=key,
                result_snapshot=json_object(response.model_dump(mode="json")),
            )
            return response

    async def call(self, step: int, call: ToolCall) -> DispatchResult:
        self.tool_calls += 1
        key = self.key(step, call.model_dump(mode="json"))
        async with self.session.begin():
            await self.lock()
            cached = await self.cached("replay.observe", key)
            if cached:
                result = DispatchResult.model_validate_json(json.dumps(cached.result_snapshot))
            else:
                try:
                    _, parameters = self.registry._get(call.function.name).prepare(
                        call.function.parsed_arguments
                    )
                except (ValueError, LookupError, TypeError):
                    parameters = call.function.parsed_arguments
                candidates = [
                    e
                    for e in self.records
                    if e.source_tool == call.function.name and e.parameters == parameters
                ]
                # 同参数多次采集时取截止点前最后一个，禁止查询现有图/Runbook/源系统。
                selected = (
                    max(candidates, key=lambda e: (e.collected_at, e.created_at, str(e.id)))
                    if candidates
                    else None
                )
                result = await self.dispatcher.dispatch(
                    task_id=self.request.task_id,
                    tool_name=call.function.name,
                    parameters=parameters,
                    actor=f"replay:{self.request.run_id}",
                    mode=DispatchMode.REPLAY,
                    replay_evidence_id=selected.id if selected else None,
                    replay_before=self.request.cutoff,
                )
                await self.ledger.append_evidence(
                    task_id=self.request.task_id,
                    source_tool="replay.observe",
                    parameters=key,
                    result_snapshot=json_object(result.model_dump(mode="json")),
                )
        if result.status is not DispatchStatus.REPLAYED or result.evidence_id is None:
            raise ReplayUnavailable("历史回放被 Policy 或 schema 门禁拒绝")
        self.observed.append(result.evidence_id)
        # 调查引擎中的 succeeded 表示观察可用；持久化结果/审计始终保留 replayed。
        return result.model_copy(update={"status": DispatchStatus.SUCCEEDED})


class ReplayStore:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        llm_factory: Callable[[ChatRequest], LLMClient] | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.llm_factory = llm_factory or (lambda r: configured_llm(settings, r))

    async def run(self, request: ReplayRequest) -> ReplayReport:
        request = ReplayRequest.model_validate(request)
        key: JsonObject = {
            "run_id": str(request.run_id),
            "request_hash": fingerprint(request.model_dump(mode="json")),
        }
        async with self.database.session() as session:
            async with session.begin():
                ledger = LedgerService(session)
                task = await session.get(AITask, request.task_id)
                if task is None or task.status is not TaskStatus.CLOSED:
                    raise ReplayUnavailable("Replay 只接受已关闭事故")
                records = await ledger.evidence_for_task(request.task_id)
                audits = await ledger.audits_for_task(request.task_id)
                for previous in records:
                    if previous.source_tool.startswith("replay.") and previous.parameters.get(
                        "run_id"
                    ) == str(request.run_id):
                        expected = (
                            key["request_hash"]
                            if previous.source_tool == "replay.report"
                            else previous.parameters.get("run_hash")
                        )
                        if expected != key["request_hash"] or (
                            previous.source_tool == "replay.report" and previous.parameters != key
                        ):
                            raise ReplayUnavailable("Replay run_id 已绑定其他输入")
                        if previous.source_tool == "replay.report":
                            return ReplayReport.model_validate_json(
                                json.dumps(previous.result_snapshot)
                            )
                baseline = await ledger.get_evidence(request.baseline_evidence_id)
                if baseline.task_id != task.id or baseline.source_tool != "agent.conclusion":
                    raise ReplayUnavailable("基线不是本事故的主 Agent 结论")
                if request.cutoff > baseline.created_at:
                    raise ReplayUnavailable("截止点不能晚于基线 RCA，避免混入处置后事实")
                original = AgentConclusion.model_validate_json(json.dumps(baseline.result_snapshot))
                matches = [
                    e
                    for e in records
                    if e.source_tool == "runbook.match"
                    and available(e, request.cutoff)
                    and int(str(e.parameters.get("phase_version", 0)))
                    < int(str(baseline.parameters["phase_version"])) - 1
                    and e.parameters.get("spec_hash")
                    == spec_hash(request.investigation.model_dump_json())
                ]
                if not matches:
                    raise ReplayUnavailable("调查规格未绑定历史 Runbook 匹配检查点")
                match = matches[-1].result_snapshot
                assert isinstance(match, dict)
                if match.get("blocked"):
                    raise ReplayUnavailable("历史 Runbook 检索失败，不能声称完成同条件回放")
                guide = (
                    RunbookView.model_validate_json(str(match["runbook_json"]))
                    if match.get("runbook_json")
                    else None
                )
                phase = int(str(baseline.parameters["phase_version"])) - 1
                phase_records = [
                    e
                    for e in records
                    if e.source_tool in {"agent.think", "agent.observe"}
                    and e.parameters.get("phase_version") == phase
                ]
                if not phase_records:
                    raise ReplayUnavailable("基线没有调查检查点")
                baseline_count = sum(e.source_tool == "agent.observe" for e in phase_records)
                elapsed = max(
                    0.0,
                    (
                        baseline.created_at - min(e.created_at for e in phase_records)
                    ).total_seconds(),
                )
                # 原任务保持 CLOSED；仅在 Ledger 留下回放起点，包含恢复等待的总耗时。
                await session.scalar(select(AITask).where(AITask.id == task.id).with_for_update())
                starts = await ledger.evidence_for_task(task.id)
                started = next(
                    (
                        e
                        for e in starts
                        if e.source_tool == "replay.start"
                        and e.parameters.get("run_id") == str(request.run_id)
                    ),
                    None,
                )
                if started is None:
                    started = await ledger.append_evidence(
                        task_id=task.id,
                        source_tool="replay.start",
                        parameters={"run_id": str(request.run_id), "run_hash": key["request_hash"]},
                        result_snapshot={"baseline_evidence_id": str(baseline.id)},
                    )
                elif started.parameters.get("run_hash") != key["request_hash"]:
                    raise ReplayUnavailable("Replay run_id 已绑定其他输入")
                human = [
                    e
                    for e in records
                    if e.source_tool == "human.answer" and available(e, request.cutoff)
                ]
                human_context = (
                    json.dumps(
                        [{"evidence_id": str(e.id), "response": e.result_snapshot} for e in human],
                        ensure_ascii=False,
                    )
                    if human
                    else None
                )
                reviews = [
                    e
                    for e in records
                    if e.source_tool == "reviewer.verdict"
                    and available(e, request.cutoff)
                    and e.parameters.get("phase_version") == phase - 1
                ]
                feedback = json.dumps(reviews[-1].result_snapshot) if reviews else None
            io = ReplayIO(session, request, self.settings, records, audits, self.llm_factory)
            candidate = None
            error = None
            try:
                result = await MainAgent().run(
                    request.investigation.model_copy(update={"max_steps": request.max_steps}),
                    io,
                    guide,
                    feedback,
                    human_context,
                )
                candidate = AgentConclusion.model_validate_json(result.conclusion_json)
            except AgentStepLimit:
                error = "step_limit"
            except ReplayUnavailable:
                error = "historical_result_unavailable"
            except InvalidConclusion:
                error = "invalid_conclusion"
            duration = max(0.0, (utc_now() - started.created_at).total_seconds())
            async with session.begin():
                await io.lock()
                records = await ledger.evidence_for_task(request.task_id)
                cached = next(
                    (
                        e
                        for e in records
                        if e.source_tool == "replay.report"
                        and e.parameters.get("run_id") == str(request.run_id)
                    ),
                    None,
                )
                if cached:
                    if cached.parameters != key:
                        raise ReplayUnavailable("Replay 输入冲突")
                    return ReplayReport.model_validate_json(json.dumps(cached.result_snapshot))
                scoring_audits = await ledger.audits_for_task(request.task_id)
                labels = [
                    e
                    for e in records
                    if e.source_tool == "evaluation.label"
                    and any(
                        a.evidence_id == e.id
                        and a.operation == "evaluation.label"
                        and a.outcome == "recorded"
                        for a in scoring_audits
                    )
                ]
                label_record = (
                    max(labels, key=lambda e: (e.created_at, str(e.id))) if labels else None
                )
                label = (
                    EvaluationLabel.model_validate_json(json.dumps(label_record.result_snapshot))
                    if label_record
                    else None
                )
                candidate_hit = (
                    root_cause_hit(candidate.root_cause.statement, label) if candidate else None
                )
                report = ReplayReport(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    cutoff=request.cutoff,
                    candidate_version=request.candidate_version,
                    baseline_evidence_id=baseline.id,
                    baseline_root_cause=original.root_cause.statement,
                    baseline_hit=root_cause_hit(original.root_cause.statement, label),
                    candidate=candidate,
                    candidate_hit=candidate_hit,
                    misjudged=(not candidate_hit if candidate_hit is not None else None),
                    label_evidence_id=label_record.id if label_record else None,
                    baseline_tool_calls=baseline_count,
                    candidate_tool_calls=io.tool_calls,
                    baseline_elapsed_seconds=elapsed,
                    candidate_elapsed_seconds=duration,
                    observed_evidence_ids=tuple(io.observed),
                    status="completed" if candidate else "incomplete",
                    error_code=error,
                )
                await ledger.append_evidence(
                    task_id=request.task_id,
                    source_tool="replay.report",
                    parameters=key,
                    result_snapshot=json_object(report.model_dump(mode="json")),
                )
                return report
