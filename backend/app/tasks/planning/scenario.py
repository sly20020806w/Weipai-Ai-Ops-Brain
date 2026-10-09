"""明确的支付回滚候选方案 Fake；仅引用当前复核上下文的真实 ID。"""

import json

from app.agent.investigation import EvidenceClaim
from app.agent.models import ChatMessage, ChatRequest, ChatResponse
from app.agent.reviewer.models import ReviewDecision
from app.policy.models import RiskLevel
from app.tasks.planning.models import ActionPlanDraft, PlannedAction, RollbackPlan, VerificationPlan


def payment_plan_response(request: ChatRequest) -> ChatResponse:
    context = json.loads(request.messages[1].content or "{}")
    if context["spec"]["service_name"] != "payment-service":
        return ChatResponse(
            id="fake-plan-unavailable",
            model="fake-payment-planner",
            finish_reason="stop",
            message=ChatMessage(role="assistant", refusal="Fake 仅支持支付回滚验收场景"),
        )
    review = ReviewDecision.model_validate_json(json.dumps(context["review"]))
    claim = EvidenceClaim(
        statement="依据连接池超时、近期变更与复核证据，提出版本回滚候选方案；执行前核对适用条件。",
        evidence_ids=tuple(sorted(review.conclusion.evidence_ids, key=str)),
    )
    draft = ActionPlanDraft(
        summary=claim,
        actions=(
            PlannedAction(
                id="rollback-payment",
                name="rollback_prod",
                service_name="payment-service",
                parameters={"from_version": "v2.3.7", "to_version": "v2.3.6"},
                risk_level=RiskLevel.L3,
                rationale=claim,
                preconditions=(
                    "核对当前版本仍为 v2.3.7，v2.3.6 的镜像、配置及发布记录可用。",
                    "确认数据库结构、配置兼容性和业务影响，通过 Policy 并取得有效审批。",
                ),
                rollback=RollbackPlan(
                    description="回滚处置自身失败或指标恶化时，停止后续动作并提交恢复原版本的候选方案。",
                    parameters={"from_version": "v2.3.6", "to_version": "v2.3.7"},
                    trigger="独立验证失败或指标恶化；再次经 Policy 和必要审批后才能恢复版本。",
                ),
                verification=VerificationPlan(
                    checks=(
                        "Deployment/Pod 版本与 Ready 状态",
                        "5xx、P99、支付成功率",
                        "Trace 与连接池超时日志",
                    ),
                    success_criteria="目标版本生效，业务指标回到故障前基线且观察窗口内无持续连接池超时。",
                    failure_response="验证失败返回调查；指标恶化停止自动处置并转交人工。",
                ),
            ),
        ),
    )
    return ChatResponse(
        id="fake-payment-plan",
        model="fake-payment-planner",
        finish_reason="stop",
        message=ChatMessage(role="assistant", content=draft.model_dump_json()),
    )
