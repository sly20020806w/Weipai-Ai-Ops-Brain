"""工单的离线 AI 脚本，只依据成功 Tool 返回的事实和 Evidence ID。"""

import json
from uuid import UUID

from app.agent.investigation import AgentConclusion, EvidenceClaim
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.tasks.tickets.models import Classification, TicketCategory, TicketContext, TicketReview
from app.tools.models import DispatchResult


def response(payload: str) -> ChatResponse:
    return ChatResponse(
        id="ticket-fake",
        model="fake-ticket-agent",
        finish_reason="stop",
        message=ChatMessage(role="assistant", content=payload),
    )


def classify_response(request: ChatRequest) -> ChatResponse:
    data = json.loads(request.messages[1].content or "{}")
    title = data["ticket"]["title"]
    category = next(
        (
            c
            for word, c in (
                ("权限", TicketCategory.PERMISSION),
                ("SQL", TicketCategory.SQL),
                ("资源", TicketCategory.RESOURCE),
                ("配置", TicketCategory.CONFIGURATION),
                ("咨询", TicketCategory.CONSULTATION),
                ("发布", TicketCategory.RELEASE),
            )
            if word in title
        ),
        TicketCategory.INCIDENT,
    )
    return response(
        Classification(
            category=category,
            rationale=EvidenceClaim(
                statement="根据工单正文归类；分类不构成授权。",
                evidence_ids=(UUID(data["evidence_id"]),),
            ),
        ).model_dump_json()
    )


def investigation_response(request: ChatRequest) -> ChatResponse:
    content = next(
        m.content
        for m in request.messages
        if m.role == "user" and m.content and '"ticket_id"' in m.content
    )
    assert content is not None
    context = TicketContext.model_validate_json(content[content.index("{") :])
    results = [
        DispatchResult.model_validate_json(m.content or "{}")
        for m in request.messages
        if m.role == "tool"
    ]
    if not results:
        return ChatResponse(
            id="ticket-read",
            model="fake-ticket-agent",
            finish_reason="tool_calls",
            message=ChatMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id="ticket-permission-read",
                        function=FunctionCall(
                            name="query_ticket_permission",
                            arguments=json.dumps(
                                {
                                    "service_name": context.service_name,
                                    "subject_id": context.request.subject_id,
                                    "resource": context.request.resource,
                                }
                            ),
                        ),
                    ),
                ),
            ),
        )
    ids = tuple(r.evidence_id for r in results if r.evidence_id)
    claim = EvidenceClaim(
        statement="当前权限已独立查询；按工单范围申请短期权限，写入仍需审批。", evidence_ids=ids
    )
    return response(
        AgentConclusion(
            root_cause=claim,
            findings=(claim,),
            confidence=0.7,
            uncertainties=("需独立复核申请范围与权限有效期，审批后验证实际结果。",),
        ).model_dump_json()
    )


def review_response(request: ChatRequest) -> ChatResponse:
    data = json.loads(request.messages[1].content or "{}")
    verdict = (
        "clear"
        if data["safe"]
        and data["permission"]["grant"] is None
        and data["ticket"]["status"] == "open"
        else "contradicted"
    )
    return response(
        TicketReview(
            task_id=UUID(data["task_id"]),
            rca_version=data["rca_version"],
            conclusion_evidence_id=UUID(data["conclusion_evidence_id"]),
            verdict=verdict,
            claim=EvidenceClaim(
                statement="重新读取工单与权限，核对请求人、资源、权限、有效期及重复授权；发现冲突即拒绝。",
                evidence_ids=tuple(UUID(i) for i in data["evidence_ids"]),
            ),
        ).model_dump_json()
    )
