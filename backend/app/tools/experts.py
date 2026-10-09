"""专家咨询是 L0 Tool，专家查询复用唯一 Dispatcher 和固定白名单。"""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.client import LLMClient
from app.agent.experts.engine import ExpertAgent
from app.agent.experts.models import (
    TOOL_ALLOWLISTS,
    ExpertAdvice,
    ExpertKind,
    ExpertOpinion,
    ExpertRequest,
)
from app.agent.experts.scenario import configured_expert_llm
from app.agent.investigation import AgentStepLimit, InvalidConclusion, InvestigationSpec
from app.agent.models import ChatRequest, ChatResponse, ToolCall, ToolDefinition, ToolFunction
from app.config import Settings
from app.connectors.holmes.factory import create_holmes_connector
from app.connectors.holmes.models import HolmesRequest
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import RiskLevel
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchResult, DispatchStatus
from app.tools.registry import ToolRegistry, json_object

ExpertLLMFactory = Callable[[ExpertKind, ChatRequest], LLMClient]


class DispatcherExpertIO:
    def __init__(self, runtime: "ExpertRuntime", kind: ExpertKind) -> None:
        self.runtime, self.kind = runtime, kind
        self.allowed = TOOL_ALLOWLISTS[kind]
        self.definitions = tuple(
            ToolDefinition(
                function=ToolFunction(
                    name=tool.name, description=tool.description, parameters=tool.input_schema
                )
            )
            for tool in runtime.registry.declarations()
            if tool.name in self.allowed
        )

    async def think(self, request: ChatRequest) -> ChatResponse:
        llm = self.runtime.llm_factory(self.kind, request)
        try:
            return await llm.chat(request)
        finally:
            await llm.aclose()

    async def call(self, call: ToolCall) -> DispatchResult:
        return await self.runtime.dispatcher.dispatch(
            task_id=self.runtime.task_id,
            tool_name=call.function.name,
            parameters=call.function.parsed_arguments,
            actor=f"expert:{self.kind.value}",
            allowed_tools=self.allowed,
        )


class ExpertRuntime:
    def __init__(
        self,
        session: AsyncSession,
        registry: ToolRegistry,
        settings: Settings,
        task_id: UUID,
        spec: InvestigationSpec,
        llm_factory: ExpertLLMFactory | None = None,
    ) -> None:
        self.registry, self.settings, self.task_id, self.spec = registry, settings, task_id, spec
        self.ledger = LedgerService(session)
        self.dispatcher = ToolDispatcher(registry, create_policy_engine(settings), self.ledger)
        self.llm_factory = llm_factory or (
            lambda kind, request: configured_expert_llm(settings, kind, request)
        )
        # 由主 Agent 宿主在每次调用前赋值，不接受模型提供的全局预算。
        self.remaining_steps = 0

    def register(self) -> None:
        if self.settings.agent_config.experts_enabled:
            self.registry.register(
                name="consult_expert",
                description=(
                    "仅在复杂调查需要专长时咨询 Kubernetes/Database/Network/Release/Security/Cost"
                    " 专家或 HolmesGPT；简单场景直接调查。返回引用证据的意见，最终结论由你判断。"
                    "HolmesGPT 必须传入本任务已成功查询的 evidence_ids，只分析快照。"
                ),
                input_model=ExpertRequest,
                output_model=ExpertAdvice,
                handler=self.consult,
                risk_level=RiskLevel.L0,
            )

    async def consult(self, request: ExpertRequest) -> ExpertAdvice:
        if (request.service_name, request.start, request.end) != (
            self.spec.service_name,
            self.spec.start,
            self.spec.end,
        ):
            raise InvalidConclusion("专家调查不能改变主任务服务或时间窗")
        budget = min(
            self.remaining_steps, self.settings.agent_config.expert_max_steps, request.max_steps
        )
        if budget < 1:
            raise AgentStepLimit("主任务没有剩余专家预算")
        if request.expert is ExpertKind.HOLMESGPT:
            return await self.holmes(request)
        return await ExpertAgent().run(
            request, DispatcherExpertIO(self, request.expert), budget=budget
        )

    async def holmes(self, request: ExpertRequest) -> ExpertAdvice:
        if not request.evidence_ids:
            raise InvalidConclusion("Holmes 必须有本任务已采集的事实快照")
        snapshots = []
        audits = await self.ledger.audits_for_task(self.task_id)
        for evidence_id in request.evidence_ids:
            evidence = await self.ledger.get_evidence(evidence_id)
            if (
                evidence.task_id != self.task_id
                or not any(
                    audit.event_type is AuditEventType.TOOL_CALL
                    and audit.evidence_id == evidence_id
                    and audit.operation == evidence.source_tool
                    and audit.outcome == DispatchStatus.SUCCEEDED.value
                    for audit in audits
                )
                or evidence.source_tool == "consult_expert"
            ):
                raise InvalidConclusion("Holmes 不能引用其他任务、检查点或专家意见冒充事实")
            snapshots.append(
                {
                    "id": str(evidence.id),
                    "tool": evidence.source_tool,
                    "parameters": evidence.parameters,
                    "result": evidence.result_snapshot,
                    "collected_at": evidence.collected_at.isoformat(),
                }
            )
        async with create_holmes_connector(self.settings) as connector:
            response = await connector.analyze(
                HolmesRequest(
                    service_name=request.service_name,
                    question=request.question,
                    context=json_object({"evidence": snapshots}),
                    output_schema=json_object(ExpertOpinion.model_json_schema()),
                )
            )
        opinion = ExpertOpinion.model_validate_json(response.analysis)
        return ExpertAdvice(
            expert=request.expert, opinion=opinion, observed_ids=request.evidence_ids, steps=1
        )
