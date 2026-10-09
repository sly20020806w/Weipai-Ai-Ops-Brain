"""完整的支付 5xx Runbook 样例，仅在隔离验收库中写入。"""

from app.policy.models import RiskLevel
from app.runbooks.schemas import (
    AutomationLevel,
    DiagnosticStep,
    HandlingStep,
    RunbookCondition,
    RunbookDraft,
    RunbookMaturity,
)
from app.tools.registry import json_object


def payment_runbook(name: str = "payment-5xx") -> RunbookDraft:
    return RunbookDraft(
        name=name,
        description="支付服务 5xx 与连接池等待超时的诊断流程",
        source="Fake 支付历史故障验收样例",
        applicability_conditions=(
            RunbookCondition(field="service_name", operator="equals", value="payment-service"),
            RunbookCondition(field="title", operator="contains", value="5xx"),
        ),
        exclusion_conditions=(RunbookCondition(field="title", operator="contains", value="维护"),),
        diagnostic_steps=tuple(
            DiagnosticStep(
                description=description,
                tool_name=name,
                parameters=json_object(parameters),
                risk_level=RiskLevel.L0,
            )
            for name, description, parameters in (
                (
                    "get_service_context",
                    "读取支付服务上下文和依赖",
                    {"service_name": "$service_name"},
                ),
                (
                    "get_recent_changes",
                    "检查近期变更",
                    {
                        "service_name": "$service_name",
                        "end": "$end",
                        "lookback_seconds": "$lookback_seconds",
                    },
                ),
                (
                    "query_metrics",
                    "查询 5xx 指标",
                    {
                        "service_name": "$service_name",
                        "start": "$start",
                        "end": "$end",
                    },
                ),
                (
                    "query_logs",
                    "查询连接池超时日志",
                    {
                        "service_name": "$service_name",
                        "start": "$start",
                        "end": "$end",
                    },
                ),
            )
        ),
        handling_steps=(
            HandlingStep(
                description="经 Reviewer、Action Plan 和审批后评估回滚上一版本",
                risk_level=RiskLevel.L3,
            ),
        ),
        risk_level=RiskLevel.L3,
        rollback_plan="若回滚引入新异常，停止自动化并由人工评估恢复原版本",
        verification_steps=("独立验证 Deployment/Pod、5xx、P99 与连接池日志恢复",),
        success_count=0,
        failure_count=0,
        confidence=0.8,
        automation_level=AutomationLevel.MANUAL,
        maturity=RunbookMaturity.VERIFIED,
    )
