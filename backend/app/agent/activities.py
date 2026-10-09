"""Temporal 调查 Activity；已提交的每轮响应与观察复用 Ledger，不重复调用。"""

import hashlib
import json
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.client import LLMClient
from app.agent.fake import FakeLLM, ScriptedChatStep, create_llm_client
from app.agent.investigation import (
    AgentConclusion,
    AgentStepLimit,
    InvalidConclusion,
    InvestigationResult,
    InvestigationSpec,
    MainAgent,
)
from app.agent.models import ChatRequest, ChatResponse, ToolCall, ToolDefinition, ToolFunction
from app.agent.reviewer.models import ReviewDecision
from app.agent.scenario import conclusion_response, tool_response
from app.agent.workflow_models import ConclusionRequest, InvestigationRequest
from app.config import Settings
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.schemas import RunbookView
from app.runbooks.workflow_models import spec_hash
from app.tasks.models import AITask
from app.tasks.states import TaskStatus
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.experts import ExpertLLMFactory, ExpertRuntime
from app.tools.models import DispatchResult, DispatchStatus, JsonObject
from app.tools.registry import ToolRegistry, json_object
from app.tools.runtime import investigation_registry


def configured_llm(settings: Settings, request: ChatRequest) -> LLMClient:
    if settings.llm_mode != "fake":
        return create_llm_client(settings)
    # 每个请求有一个显式脚本步骤。恢复时根据持久化的对话位置取下一条。
    index = sum(message.role == "tool" for message in request.messages)
    return FakeLLM(
        [
            ScriptedChatStep(
                (lambda value: tool_response(value, index)) if index < 4 else conclusion_response
            )
        ]
    )


async def lock_task(session: AsyncSession, expected: TaskSnapshot) -> AITask:
    task = await session.scalar(
        select(AITask).where(AITask.id == UUID(expected.task_id)).with_for_update()
    )
    if task is None or (task.status, task.status_version) != (expected.status, expected.version):
        raise InvalidConclusion("调查任务状态或版本已改变")
    return task


class DatabaseInvestigationIO:
    def __init__(
        self,
        session: AsyncSession,
        registry: ToolRegistry,
        settings: Settings,
        task: TaskSnapshot,
        llm_factory: Callable[[ChatRequest], LLMClient],
        expert_runtime: ExpertRuntime | None = None,
        *,
        checkpoint_prefix: str = "agent",
        actor: str = "codex-main-agent",
        allowed_tools: frozenset[str] | None = None,
        call_scope: Callable[[ToolCall], bool] | None = None,
    ) -> None:
        self.session, self.task, self.llm_factory = session, task, llm_factory
        self.runbook_maturity_config = settings.runbook_maturity_config
        self.expert_runtime = expert_runtime
        self.checkpoint_prefix, self.actor, self.allowed_tools = (
            checkpoint_prefix,
            actor,
            allowed_tools,
        )
        self.call_scope = call_scope
        self.ledger = LedgerService(session)
        self.dispatcher = ToolDispatcher(registry, create_policy_engine(settings), self.ledger)
        self.definitions = tuple(
            ToolDefinition(
                function=ToolFunction(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.input_schema,
                )
            )
            for tool in registry.declarations()
            if allowed_tools is None or tool.name in allowed_tools
        )

    def key(self, step: int, payload: str) -> JsonObject:
        return {
            "phase_version": self.task.version,
            "step": step,
            "request_hash": hashlib.sha256(
                json.dumps(
                    json.loads(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        }

    async def cached(self, source: str, key: JsonObject) -> Evidence | None:
        evidence = await self.session.scalar(
            select(Evidence).where(
                Evidence.task_id == UUID(self.task.task_id),
                Evidence.source_tool == source,
                Evidence.parameters["phase_version"].as_integer() == self.task.version,
                Evidence.parameters["step"].as_integer() == key["step"],
            )
        )
        if evidence is not None and evidence.parameters != key:
            raise InvalidConclusion("调查检查点请求冲突")
        return evidence

    async def think(self, step: int, request: ChatRequest) -> ChatResponse:
        key = self.key(step, request.model_dump_json())
        async with self.session.begin():
            await lock_task(self.session, self.task)
            cached = await self.cached(f"{self.checkpoint_prefix}.think", key)
            if cached is not None:
                return ChatResponse.model_validate_json(_snapshot_json(cached))
            llm = self.llm_factory(request)
            try:
                response = await llm.chat(request)
                response = ChatResponse.model_validate_json(response.model_dump_json())
            finally:
                await llm.aclose()
            await self.ledger.append_evidence(
                task_id=UUID(self.task.task_id),
                source_tool=f"{self.checkpoint_prefix}.think",
                parameters=key,
                result_snapshot=json_object(response.model_dump(mode="json")),
            )
            return response

    async def call(self, step: int, call: ToolCall) -> DispatchResult:
        key = self.key(step, call.model_dump_json())
        async with self.session.begin():
            await lock_task(self.session, self.task)
            cached = await self.cached(f"{self.checkpoint_prefix}.observe", key)
            if cached is not None:
                return DispatchResult.model_validate_json(_snapshot_json(cached))
            if self.expert_runtime is not None:
                self.expert_runtime.remaining_steps = self.expert_runtime.spec.max_steps - step
            result = await self.dispatcher.dispatch(
                task_id=UUID(self.task.task_id),
                tool_name=call.function.name,
                parameters=call.function.parsed_arguments,
                actor=self.actor,
                allowed_tools=frozenset()
                if self.call_scope is not None and not self.call_scope(call)
                else self.allowed_tools,
            )
            observation = await self.ledger.append_evidence(
                task_id=UUID(self.task.task_id),
                source_tool=f"{self.checkpoint_prefix}.observe",
                parameters=key,
                result_snapshot=json_object(result.model_dump(mode="json")),
            )
            if (
                self.checkpoint_prefix == "agent"
                and call.id.startswith("runbook-")
                and result.status is DispatchStatus.FAILED
            ):
                from app.runbooks.lifecycle import RunbookLifecycle

                task = await self.session.get(AITask, UUID(self.task.task_id))
                assert task is not None
                await RunbookLifecycle(
                    self.session, self.runbook_maturity_config
                ).record_diagnostic_failure(task, observation.id)
            return result


def _snapshot_json(evidence: Evidence) -> str:
    return json.dumps(evidence.result_snapshot, ensure_ascii=False)


class AgentActivities:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        llm_factory: Callable[[ChatRequest], LLMClient] | None = None,
        expert_llm_factory: ExpertLLMFactory | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.llm_factory = llm_factory or (lambda request: configured_llm(settings, request))
        self.expert_llm_factory = expert_llm_factory

    @activity.defn(name="agent.investigate")
    async def investigate(self, request: InvestigationRequest) -> InvestigationResult:
        try:
            spec = InvestigationSpec.model_validate_json(request.spec_json)
            if request.task.status is not TaskStatus.INVESTIGATING:
                raise InvalidConclusion("主 Agent 只能在 INVESTIGATING 阶段运行")
            async with self.database.session() as session:
                guide = None
                feedback = None
                human_context = None
                if request.runbook_json is not None:
                    guide = RunbookView.model_validate_json(request.runbook_json)
                async with session.begin():
                    await lock_task(session, request.task)
                    if request.review_evidence_id is not None:
                        # 反证反馈只接受紧邻本次重新调查的持久化 Reviewer 结果。
                        previous_review = await LedgerService(session).get_evidence(
                            UUID(request.review_evidence_id)
                        )
                        if (
                            previous_review.task_id != UUID(request.task.task_id)
                            or previous_review.source_tool != "reviewer.verdict"
                            or previous_review.parameters.get("phase_version")
                            != request.task.version - 1
                            or not isinstance(previous_review.result_snapshot, dict)
                        ):
                            raise InvalidConclusion("Reviewer 反馈不是本次重新调查的反证")
                        previous_decision = ReviewDecision.model_validate_json(
                            json.dumps(previous_review.result_snapshot)
                        )
                        if previous_decision.report.verdict != "contradicted":
                            raise InvalidConclusion("重新调查只接受已提交的反证反馈")
                        feedback = json.dumps(
                            previous_review.result_snapshot, ensure_ascii=False, sort_keys=True
                        )
                    checkpoint = await session.scalar(
                        select(Evidence).where(
                            Evidence.task_id == UUID(request.task.task_id),
                            Evidence.source_tool == "runbook.match",
                            Evidence.parameters["phase_version"].as_integer()
                            == request.task.version - 1,
                        )
                    )
                    if checkpoint is None and guide is not None:
                        # 问答暂停会增加状态版本；只复用本任务同一调查的已验证匹配快照。
                        answered = await session.scalar(
                            select(Evidence).where(
                                Evidence.task_id == UUID(request.task.task_id),
                                Evidence.source_tool == "human.answer",
                                Evidence.parameters["prompt"]["task"]["version"].as_integer()
                                == request.task.version - 1,
                                Evidence.parameters["prompt"]["resume_status"].as_string()
                                == TaskStatus.INVESTIGATING.value,
                            )
                        )
                        if answered is None:
                            raise InvalidConclusion("旧 Runbook 快照不能跨调查阶段复用")
                        checkpoint = await session.scalar(
                            select(Evidence)
                            .where(
                                Evidence.task_id == UUID(request.task.task_id),
                                Evidence.source_tool == "runbook.match",
                                Evidence.parameters["phase_version"].as_integer()
                                == request.task.version - 3,
                            )
                            .order_by(Evidence.parameters["phase_version"].as_integer().desc())
                            .limit(1)
                        )
                    if checkpoint is None:
                        if guide is not None:
                            raise InvalidConclusion("Runbook 并非本任务已提交的匹配结果")
                    elif (
                        not isinstance(checkpoint.result_snapshot, dict)
                        or checkpoint.result_snapshot.get("blocked") is True
                        or checkpoint.parameters.get("spec_hash") != spec_hash(request.spec_json)
                        or checkpoint.result_snapshot.get("runbook_json") != request.runbook_json
                    ):
                        raise InvalidConclusion("Runbook 并非本任务已提交的匹配结果")
                    answers = await session.scalars(
                        select(Evidence)
                        .where(
                            Evidence.task_id == UUID(request.task.task_id),
                            Evidence.source_tool == "human.answer",
                            Evidence.parameters["prompt"]["task"]["version"].as_integer()
                            < request.task.version,
                        )
                        .order_by(Evidence.parameters["prompt"]["task"]["version"].as_integer())
                    )
                    records = [
                        {"evidence_id": str(item.id), "response": item.result_snapshot}
                        for item in answers
                    ]
                    if records:
                        human_context = json.dumps(records, ensure_ascii=False, sort_keys=True)
                    chat_record = await session.scalar(
                        select(Evidence).where(
                            Evidence.task_id == UUID(request.task.task_id),
                            Evidence.source_tool == "chat.request",
                        )
                    )
                    if chat_record is not None:
                        from app.agent.chat.service import chat_input, read_answer

                        _, submission = await chat_input(session, UUID(request.task.task_id))
                        previous = None
                        if submission.input.previous_task_id is not None:
                            previous = await read_answer(session, submission.input.previous_task_id)
                        human_context = json.dumps(
                            {
                                "message": submission.input.message,
                                "mode": submission.input.mode,
                                "previous_answer": previous.model_dump(mode="json")
                                if previous
                                else None,
                                "instruction": (
                                    "历史回答仅作为追问背景，本轮判断须重新查询并引用本轮证据；"
                                    "对话不构成动作审批。"
                                ),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                async with investigation_registry(self.settings, session) as registry:
                    expert = ExpertRuntime(
                        session,
                        registry,
                        self.settings,
                        UUID(request.task.task_id),
                        spec,
                        self.expert_llm_factory,
                    )
                    expert.register()
                    io = DatabaseInvestigationIO(
                        session, registry, self.settings, request.task, self.llm_factory, expert
                    )
                    return await MainAgent().run(spec, io, guide, feedback, human_context)
        except AgentStepLimit:
            raise ApplicationError(
                "主 Agent 超过最大调查步数", type="AgentStepLimit", non_retryable=True
            ) from None
        except (InvalidConclusion, ValueError):
            raise ApplicationError(
                "主 Agent 结论或调查输入被拒绝", type="InvalidConclusion", non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("主 Agent 调查失败") from None

    @activity.defn(name="agent.validate_conclusion")
    async def validate_conclusion(self, request: ConclusionRequest) -> str:
        try:
            if request.task.status is not TaskStatus.RCA:
                raise InvalidConclusion("只有 RCA 阶段可接受调查结论")
            conclusion = AgentConclusion.model_validate_json(request.result.conclusion_json)
            async with self.database.session() as session, session.begin():
                await lock_task(session, request.task)
                ledger = LedgerService(session)
                evidence = await ledger.evidence_for_task(UUID(request.task.task_id))
                observations = sorted(
                    (
                        item
                        for item in evidence
                        if item.source_tool == "agent.observe"
                        and item.parameters.get("phase_version") == request.task.version - 1
                    ),
                    key=lambda item: int(str(item.parameters["step"])),
                )
                observed_ids = []
                for item in observations:
                    result = DispatchResult.model_validate_json(_snapshot_json(item))
                    if result.status is DispatchStatus.SUCCEEDED and result.evidence_id is not None:
                        observed_ids.append(str(result.evidence_id))
                if request.result.observed_ids != observed_ids:
                    raise InvalidConclusion("结论观察清单与持久化记录不符")
                if not conclusion.evidence_ids <= {UUID(value) for value in observed_ids}:
                    raise InvalidConclusion("结论引用未观察的证据")
                audits = await ledger.audits_for_task(UUID(request.task.task_id))
                for evidence_id in conclusion.evidence_ids:
                    referenced = await ledger.get_evidence(evidence_id)
                    if referenced.task_id != UUID(request.task.task_id) or not any(
                        audit.event_type is AuditEventType.TOOL_CALL
                        and audit.actor == "codex-main-agent"
                        and audit.evidence_id == evidence_id
                        and audit.operation == referenced.source_tool
                        and audit.outcome == DispatchStatus.SUCCEEDED.value
                        for audit in audits
                    ):
                        raise InvalidConclusion("引用不是本任务成功调用产生的真实证据")
                parameters = json_object(
                    {
                        "phase_version": request.task.version,
                        "steps": request.result.steps,
                        "observed_ids": observed_ids,
                    }
                )
                snapshot = json_object(conclusion.model_dump(mode="json"))
                previous = next(
                    (
                        item
                        for item in evidence
                        if item.source_tool == "agent.conclusion"
                        and item.parameters.get("phase_version") == request.task.version
                    ),
                    None,
                )
                if previous is not None:
                    if previous.parameters != parameters or previous.result_snapshot != snapshot:
                        raise InvalidConclusion("同一 RCA 版本的结论发生冲突")
                    return str(previous.id)
                accepted = await ledger.append_evidence(
                    task_id=UUID(request.task.task_id),
                    source_tool="agent.conclusion",
                    parameters=parameters,
                    result_snapshot=snapshot,
                )
                return str(accepted.id)
        except (InvalidConclusion, ValueError, LookupError):
            raise ApplicationError(
                "RCA 结论证据校验失败", type="InvalidConclusion", non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("RCA 结论保存失败") from None
