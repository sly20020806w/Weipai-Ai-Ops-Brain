"""主 Agent 按需咨询 Database 和 Holmes 后综合意见的 Fake 场景。"""

from app.agent.experts.models import ExpertKind, ExpertRequest
from app.agent.investigation import AgentConclusion, EvidenceClaim
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.agent.scenario import script_spec, tool_response
from app.tools.models import DispatchResult, DispatchStatus


def consultation_response(request: ChatRequest) -> ChatResponse:
    results = [
        DispatchResult.model_validate_json(message.content or "{}")
        for message in request.messages
        if message.role == "tool"
    ]
    index = len(results)
    if index < 4:
        return tool_response(request, index)
    if index < 6:
        spec = script_spec(request)
        expert = ExpertKind.DATABASE if index == 4 else ExpertKind.HOLMESGPT
        evidence_ids = (
            tuple(item.evidence_id for item in results[2:4] if item.evidence_id is not None)
            if index == 5
            else ()
        )
        consult = ExpertRequest(
            **spec.model_dump(),
            expert=expert,
            question="连接池变更与数据库容量是否相关，需要哪些反证？",
            evidence_ids=evidence_ids,
        )
        return ChatResponse(
            id=f"fake-consult-{index}",
            model="fake-main",
            finish_reason="tool_calls",
            message=ChatMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id=f"consult-{index}",
                        function=FunctionCall(
                            name="consult_expert", arguments=consult.model_dump_json()
                        ),
                    ),
                ),
            ),
        )
    if any(item.status is not DispatchStatus.SUCCEEDED for item in results):
        return ChatResponse(
            id="fake-no-opinion",
            model="fake-main",
            finish_reason="stop",
            message=ChatMessage(role="assistant", refusal="专家没有有效意见"),
        )
    ids = tuple(item.evidence_id for item in results if item.evidence_id is not None)
    conclusion = AgentConclusion(
        root_cause=EvidenceClaim(
            statement="主 Agent 综合证据和专家意见：连接池扩大是数据库压力的待验证线索。",
            evidence_ids=ids,
        ),
        findings=(
            EvidenceClaim(
                statement="专家意见支持继续核查容量，仍应排查网络和依赖等替代原因。",
                evidence_ids=ids[-2:],
            ),
        ),
        confidence=0.75,
        uncertainties=("尚未完成 Reviewer 反证、处置与独立验证。",),
    )
    return ChatResponse(
        id="fake-main-decision",
        model="fake-main",
        finish_reason="stop",
        message=ChatMessage(role="assistant", content=conclusion.model_dump_json()),
    )
