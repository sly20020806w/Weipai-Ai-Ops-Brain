"""从已接受的 RCA、执行回执和独立验证合成复盘，不把缺失事实编成原因。"""

import json
from uuid import UUID

from app.agent.investigation import AgentConclusion, EvidenceClaim
from app.learning.models import SECTIONS, PostmortemDraft, PostmortemSection
from app.ledger.models import Evidence
from app.policy.models import RiskLevel
from app.runbooks.schemas import (
    AutomationLevel,
    DiagnosticStep,
    HandlingStep,
    RunbookCondition,
    RunbookDraft,
    RunbookMaturity,
)
from app.tasks.planning.models import ActionPlan
from app.verifier.models import VerificationReport


def compose(
    context_id: UUID,
    title: str,
    service_name: str,
    records: list[Evidence],
) -> tuple[PostmortemDraft, RunbookDraft]:
    def latest(source: str) -> Evidence | None:
        return next((e for e in reversed(records) if e.source_tool == source), None)

    def claim(statement: str, *ids: UUID) -> EvidenceClaim:
        return EvidenceClaim(statement=statement[:4000], evidence_ids=tuple(dict.fromkeys(ids)))

    conclusion_record, plan_record, verification_record = (
        latest("agent.conclusion"),
        latest("action_plan"),
        latest("verify_action"),
    )
    conclusion = (
        AgentConclusion.model_validate_json(json.dumps(conclusion_record.result_snapshot))
        if conclusion_record
        else None
    )
    plan = (
        ActionPlan.model_validate_json(json.dumps(plan_record.result_snapshot))
        if plan_record
        else None
    )
    verification = (
        VerificationReport.model_validate_json(json.dumps(verification_record.result_snapshot))
        if verification_record
        else None
    )
    root = (
        claim(
            conclusion.root_cause.statement,
            conclusion_record.id,
            *conclusion.root_cause.evidence_ids,
        )
        if conclusion and conclusion_record
        else claim("根因尚未确认；当前证据不足，需补充调查。", context_id)
    )
    actions = [e for e in records if e.source_tool == "execute_action"]
    handling = (
        claim(
            "已提交的动作回执："
            + "；".join(str(e.parameters.get("name", "动作")) for e in actions),
            *(e.id for e in actions),
        )
        if actions
        else claim("当前事故没有实际动作回执；不推断已执行任何处置。", context_id)
    )
    verified = (
        claim(
            "独立 Verifier "
            + ("全部八项检查通过。" if verification.passed else "检查未全部通过。")
            + "；".join(f"{c.name}：{c.reason}" for c in verification.checks),
            verification_record.id,
            *(c.evidence_id for c in verification.checks if c.evidence_id),
        )
        if verification and verification_record
        else claim("没有独立验证报告，不能声称业务已经恢复。", context_id)
    )
    monitoring = claim(
        "建议补齐业务 5xx、P99、成功率与数据库连接余量的关联监控。",
        *(verified.evidence_ids or (context_id,)),
    )
    alerting = claim(
        "建议结合本次事故 Timeline 复核告警阈值与触发延迟，并用历史证据验证。",
        context_id,
        *root.evidence_ids,
    )
    architecture = claim(
        "建议围绕已记录根因评审容量约束与依赖隔离；尚未验证的方案需单独评审。", *root.evidence_ids
    )
    automation = claim(
        "建议将本次只读诊断、独立验证与回滚前置检查固化，写操作仍须 Policy 和审批。",
        *handling.evidence_ids,
        *verified.evidence_ids,
    )
    claims = (
        claim(title, context_id),
        claim(
            f"已确认关联服务：{service_name}；没有证据的用户数、金额和其他服务影响不作推断。",
            context_id,
        ),
        claim("参见按 UTC 排序的事件、状态迁移与证据采集 Timeline。", context_id),
        claim(
            "本报告逐条引用本事故持久化的事实、RCA、处理与验证证据。",
            context_id,
            *root.evidence_ids,
            *handling.evidence_ids,
            *verified.evidence_ids,
        ),
        root,
        handling,
        verified,
        claim(
            "现有证据不足以证明未提前发现的具体原因；需核对监控覆盖、告警阈值和发布验证时机。",
            context_id,
        ),
        monitoring,
        alerting,
        architecture,
        automation,
        claim(
            "从本次证据生成 Draft Runbook；尚未审核或独立验证，不授予自动执行权限。",
            *root.evidence_ids,
            *handling.evidence_ids,
        ),
    )
    draft = PostmortemDraft(
        sections=tuple(
            PostmortemSection(title=t, conclusions=(c,))
            for t, c in zip(SECTIONS, claims, strict=True)
        ),
        improvements=(monitoring, alerting, architecture, automation),
    )
    diagnostic = []
    for record in records:
        if record.source_tool in {
            "get_service_context",
            "get_recent_changes",
            "query_metrics",
            "query_logs",
        }:
            params = dict(record.parameters)
            for key, placeholder in (
                ("service_name", "$service_name"),
                ("start", "$start"),
                ("end", "$end"),
            ):
                if key in params:
                    params[key] = placeholder
            diagnostic.append(
                DiagnosticStep(
                    description=f"按事故证据 {record.id} 复查 {record.source_tool}",
                    tool_name=record.source_tool,
                    parameters=params,
                    risk_level=RiskLevel.L0,
                )
            )
    if not diagnostic:
        diagnostic.append(
            DiagnosticStep(
                description="先读取当前服务上下文，再人工补齐根因证据。",
                tool_name="get_service_context",
                parameters={"service_name": "$service_name"},
                risk_level=RiskLevel.L0,
            )
        )
    runbook = RunbookDraft(
        name=f"incident-{context_id.hex}",
        description=root.statement,
        source=f"事故服务 {service_name}；上下文 Evidence {context_id}",
        applicability_conditions=(
            RunbookCondition(field="service_name", operator="equals", value=service_name),
        ),
        exclusion_conditions=(),
        diagnostic_steps=tuple(diagnostic[:30]),
        handling_steps=tuple(
            HandlingStep(
                description=(
                    f"{a.action.name} {a.action.service_name} "
                    f"{json.dumps(a.action.parameters, ensure_ascii=False)}；"
                    f"{a.action.rationale.statement}；Evidence："
                    + ", ".join(map(str, a.action.rationale.evidence_ids))
                )[:4000],
                risk_level=a.action.risk_level,
            )
            for a in plan.actions
        )
        if plan
        else (HandlingStep(description="根因或方案不充分，转人工判断。", risk_level=RiskLevel.L5),),
        risk_level=max(a.action.risk_level for a in plan.actions) if plan else RiskLevel.L5,
        rollback_plan="；".join(a.action.rollback.description for a in plan.actions)[:4000]
        if plan
        else "当前无已验证回滚方案；须人工制定并经 Policy 和审批后执行。",
        verification_steps=tuple(
            f"{c.name}：{c.reason}；Evidence：{c.evidence_id}" for c in verification.checks
        )
        if verification
        else ("必须经独立 Verifier 验证，不以执行回执代替业务恢复。",),
        success_count=0,
        failure_count=0,
        confidence=conclusion.confidence if conclusion else 0.0,
        automation_level=AutomationLevel.MANUAL,
        maturity=RunbookMaturity.DRAFT,
    )
    return draft, runbook
