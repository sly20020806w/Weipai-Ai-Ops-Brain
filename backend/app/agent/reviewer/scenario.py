"""支付场景 Fake Reviewer 根据真实返回快照判断，不预先写死复核结论。"""

import json
import math

from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.agent.reviewer.models import AlternativeCause, ReviewCheck, ReviewInput, ReviewReport
from app.tools.models import DispatchResult, DispatchStatus, JsonObject


def review_response(request: ChatRequest) -> ChatResponse:
    value = ReviewInput.model_validate_json(request.messages[1].content or "{}")
    results = [
        DispatchResult.model_validate_json(item.content or "{}")
        for item in request.messages
        if item.role == "tool"
    ]
    if len(results) < 2:
        index = len(results)
        parameters: JsonObject = {"service_name": value.spec.service_name}
        if index == 0:
            parameters.update(start=value.spec.start.isoformat(), end=value.spec.end.isoformat())
        else:
            parameters.update(
                end=value.spec.end.isoformat(),
                lookback_seconds=math.floor((value.spec.end - value.spec.start).total_seconds()),
            )
        return ChatResponse(
            id=f"review-query-{index}",
            model="fake-reviewer",
            finish_reason="tool_calls",
            message=ChatMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id=f"review-{index}",
                        function=FunctionCall(
                            name="query_traces" if index == 0 else "get_recent_changes",
                            arguments=json.dumps(parameters),
                        ),
                    ),
                ),
            ),
        )
    if any(
        item.status is not DispatchStatus.SUCCEEDED or item.evidence_id is None for item in results
    ):
        return ChatResponse(
            id="review-refusal",
            model="fake-reviewer",
            finish_reason="stop",
            message=ChatMessage(role="assistant", refusal="独立证据不足"),
        )
    traces = (results[0].result or {}).get("traces", [])
    spans: list[JsonObject] = []
    if isinstance(traces, list):
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            trace_spans = trace.get("spans", [])
            if isinstance(trace_spans, list):
                spans.extend(span for span in trace_spans if isinstance(span, dict))
    checks = []
    for alternative, operation in (
        (AlternativeCause.NETWORK, "TCP connect"),
        (AlternativeCause.REDIS, "Redis GET"),
        (AlternativeCause.THIRD_PARTY, "第三方支付请求"),
    ):
        samples = [span for span in spans if span.get("operation") == operation]
        contradicted = any(
            span.get("result_code") not in {"ok", "200", "success"} for span in samples
        )
        checks.append(
            ReviewCheck(
                alternative=alternative,
                outcome="contradicts"
                if contradicted
                else "not_supported"
                if samples
                else "inconclusive",
                statement=f"{operation} 观测出现失败，反驳仅归因于连接池的结论。"
                if contradicted
                else f"查询范围内 {operation} 样本正常，暂未发现该替代原因。"
                if samples
                else f"缺少 {operation} 样本，不能排除该原因。",
                evidence_ids=(results[0].evidence_id,),  # type: ignore[arg-type]
            )
        )
    events = (results[1].result or {}).get("events", [])
    checks.append(
        ReviewCheck(
            alternative=AlternativeCause.RELEASE,
            outcome="not_supported" if events else "inconclusive",
            statement="近期变更已重新查询，暂未找到与原发布线索冲突的其他变更；因果关系仍待调查。"
            if events
            else "缺少变更样本，不能排除其他发布原因。",
            evidence_ids=(results[1].evidence_id,),  # type: ignore[arg-type]
        )
    )
    return ChatResponse(
        id="review-report",
        model="fake-reviewer",
        finish_reason="stop",
        message=ChatMessage(
            role="assistant", content=ReviewReport(checks=tuple(checks)).model_dump_json()
        ),
    )
