"""支付 5xx 的有限离线 LLM 脚本，只引用实际 Tool 返回的证据。"""

import json
import math
from functools import partial
from typing import Literal

from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import AgentConclusion, EvidenceClaim, InvestigationSpec
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.tools.models import DispatchResult, DispatchStatus, JsonObject

PAYMENT_TOOLS = ("get_service_context", "get_recent_changes", "query_metrics", "query_logs")


def script_spec(request: ChatRequest) -> InvestigationSpec:
    content = request.messages[1].content
    assert content is not None
    return InvestigationSpec.model_validate_json(content)


def tool_response(request: ChatRequest, index: int) -> ChatResponse:
    spec = script_spec(request)
    parameters: JsonObject = {"service_name": spec.service_name}
    if index == 1:
        parameters.update(
            lookback_seconds=math.ceil((spec.end - spec.start).total_seconds()),
            end=spec.end.isoformat(),
        )
    elif index >= 2:
        parameters.update(start=spec.start.isoformat(), end=spec.end.isoformat())
    return ChatResponse(
        id=f"fake-turn-{index}",
        model="fake-payment-investigator",
        finish_reason="tool_calls",
        message=ChatMessage(
            role="assistant",
            content="按计划查询下一项事实，再结合返回证据继续判断。",
            tool_calls=(
                ToolCall(
                    id=f"payment-query-{index}",
                    function=FunctionCall(
                        name=PAYMENT_TOOLS[index], arguments=json.dumps(parameters)
                    ),
                ),
            ),
        ),
    )


def conclusion_response(request: ChatRequest) -> ChatResponse:
    results = [
        DispatchResult.model_validate_json(message.content or "{}")
        for message in request.messages
        if message.role == "tool"
    ]
    if len(results) != 4 or any(
        item.status is not DispatchStatus.SUCCEEDED or item.evidence_id is None for item in results
    ):
        return ChatResponse(
            id="fake-insufficient",
            model="fake-payment-investigator",
            finish_reason="stop",
            message=ChatMessage(role="assistant", refusal="查询失败，证据不足以支持结论"),
        )
    snapshots = [item.result or {} for item in results]
    if script_spec(request).service_name != "payment-service":
        return ChatResponse(
            id="fake-out-of-scope",
            model="fake-payment-investigator",
            finish_reason="stop",
            message=ChatMessage(role="assistant", refusal="离线脚本只支持支付验收场景"),
        )
    if not all(
        snapshots[index].get(key) for index, key in enumerate(("nodes", "events", "series", "logs"))
    ):
        return ChatResponse(
            id="fake-empty",
            model="fake-payment-investigator",
            finish_reason="stop",
            message=ChatMessage(role="assistant", refusal="样例所需事实缺失，不能下结论"),
        )
    ids = [item.evidence_id for item in results]
    assert all(value is not None for value in ids)
    claims = tuple(
        EvidenceClaim(statement=statement, evidence_ids=(evidence_id,))
        for statement, evidence_id in zip(
            (
                "认知图已提供 payment-service 的运行资源与依赖关系，需注意关系新鲜度。",
                "查询时间窗内存在近期变更，需要继续验证其与故障的因果关系。",
                "查询窗口内返回了 payment-service 的 5xx 指标。",
                "日志返回数据库连接池等待超时。",
            ),
            ids,
            strict=True,
        )
        if evidence_id is not None
    )
    conclusion = AgentConclusion(
        root_cause=EvidenceClaim(
            statement="数据库连接池等待超时是当前故障线索，近期发布为待验证的诱因。",
            evidence_ids=tuple(value for value in ids[1:] if value is not None),
        ),
        findings=claims,
        confidence=0.7,
        uncertainties=("近期发布与故障的因果关系仍需核实；生产恢复以独立 Verifier 结果为准。",),
    )
    return ChatResponse(
        id="fake-conclusion",
        model="fake-payment-investigator",
        finish_reason="stop",
        message=ChatMessage(role="assistant", content=conclusion.model_dump_json()),
    )


def invalid_conclusion_response(request: ChatRequest) -> ChatResponse:
    response = conclusion_response(request)
    data = json.loads(response.message.content or "{}")
    data["root_cause"]["evidence_ids"] = ["00000000-0000-0000-0000-000000000001"]
    return response.model_copy(
        update={
            "message": ChatMessage(role="assistant", content=json.dumps(data, ensure_ascii=False))
        }
    )


def payment_llm(mode: Literal["valid", "invalid"] = "valid") -> FakeLLM:
    steps = [ScriptedChatStep(partial(tool_response, index=index)) for index in range(4)]
    if mode == "invalid":
        steps.append(ScriptedChatStep(invalid_conclusion_response))
    else:
        steps.append(ScriptedChatStep(conclusion_response))
    return FakeLLM(steps)
