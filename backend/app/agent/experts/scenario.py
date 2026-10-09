"""专家 Fake 脚本从实际观察构造意见，不预填 Evidence ID。"""

import json

from app.agent.client import LLMClient
from app.agent.experts.models import ExpertKind, ExpertOpinion, ExpertRequest
from app.agent.fake import FakeLLM, ScriptedChatStep, create_llm_client
from app.agent.investigation import EvidenceClaim
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.config import Settings
from app.tools.models import DispatchResult, DispatchStatus


def expert_response(kind: ExpertKind, request: ChatRequest) -> ChatResponse:
    results = [
        DispatchResult.model_validate_json(message.content or "{}")
        for message in request.messages
        if message.role == "tool"
    ]
    spec = ExpertRequest.model_validate_json(request.messages[1].content or "{}")
    if not results:
        name = {
            ExpertKind.KUBERNETES: "get_k8s_status",
            ExpertKind.DATABASE: "query_metrics",
            ExpertKind.NETWORK: "query_traces",
            ExpertKind.RELEASE: "get_recent_changes",
            ExpertKind.SECURITY: "query_logs",
            ExpertKind.COST: "get_cloud_resources",
        }[kind]
        parameters: dict[str, object] = {"service_name": spec.service_name}
        if name == "get_k8s_status":
            parameters["namespace"] = "payment"
        elif name == "get_recent_changes":
            parameters.update(
                lookback_seconds=int((spec.end - spec.start).total_seconds()),
                end=spec.end.isoformat(),
            )
        elif name in {"query_metrics", "query_traces", "query_logs", "get_cloud_resources"}:
            parameters.update(start=spec.start.isoformat(), end=spec.end.isoformat())
        return ChatResponse(
            id="fake-expert-query",
            model="fake-expert",
            finish_reason="tool_calls",
            message=ChatMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id="expert-query",
                        function=FunctionCall(name=name, arguments=json.dumps(parameters)),
                    ),
                ),
            ),
        )
    ids = tuple(
        item.evidence_id
        for item in results
        if item.status is DispatchStatus.SUCCEEDED and item.evidence_id is not None
    )
    if not ids:
        return ChatResponse(
            id="fake-expert-refusal",
            model="fake-expert",
            finish_reason="stop",
            message=ChatMessage(role="assistant", refusal="查询被拒绝，专家没有足够证据"),
        )
    opinion = ExpertOpinion(
        assessment=EvidenceClaim(
            statement=f"{kind.value} 专家建议结合已返回数据核查 payment-service 的容量与依赖。",
            evidence_ids=ids,
        ),
        confidence=0.7,
        uncertainties=("意见需主 Agent 综合判断，尚未验证因果。",),
    )
    return ChatResponse(
        id="fake-expert-opinion",
        model="fake-expert",
        finish_reason="stop",
        message=ChatMessage(role="assistant", content=opinion.model_dump_json()),
    )


def configured_expert_llm(settings: Settings, kind: ExpertKind, request: ChatRequest) -> LLMClient:
    if settings.llm_mode != "fake":
        return create_llm_client(settings)
    return FakeLLM([ScriptedChatStep(lambda value: expert_response(kind, value))])
