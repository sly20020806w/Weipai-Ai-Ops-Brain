"""纯规则评估：缺数据、过期、未来观测和范围不完整均不等于健康。"""

from datetime import datetime
from uuid import UUID

from app.connectors.inspection.models import InspectionFact, InspectionFacts
from app.tasks.inspection.catalog import BY_ID, Mode, Outcome, outcome, selected_checks
from app.tasks.inspection.models import CheckResult, InspectionConfig


def evaluate(
    service: str,
    mode: Mode,
    config: InspectionConfig,
    facts: InspectionFacts | None,
    evidence_id: UUID | None,
    now: datetime,
) -> tuple[CheckResult, ...]:
    config = InspectionConfig.model_validate(config)
    if facts is not None:
        facts = InspectionFacts.model_validate(facts)
        if facts.service_name != service or any(f.check_id not in BY_ID for f in facts.facts):
            raise ValueError("巡检事实服务或检查类型不匹配")
    result = []
    for check in selected_checks(mode):
        matching = [f for f in facts.facts if f.check_id == check.id] if facts else []
        observations: list[InspectionFact | None] = list(matching) or [None]
        for fact in observations:
            reason = "测量符合检查规则"
            state: Outcome = "unknown"
            if facts is None:
                reason = "事实查询失败或被 Policy 拒绝，需要补充可核验数据"
            elif not facts.complete:
                reason = "来源范围或分页不完整，需要补充可核验数据"
            elif fact is None:
                reason = "缺少检查事实，需要补充可核验数据"
            elif not 0 <= (now - fact.observed_at).total_seconds() <= config.max_age_seconds:
                reason = "观测过期或来自未来，需要重新采集"
            else:
                state = outcome(check, fact.value, config.thresholds.get(check.id, check.threshold))
                if state == "abnormal":
                    reason = "测量触发检查规则；处理仍需调查与 Policy 授权"
                elif state == "unknown":
                    reason = "测量缺失或类型不匹配，需要补充可核验数据"
            result.append(
                CheckResult(
                    service_name=service,
                    check_id=check.id,
                    resource=fact.resource if fact else service,
                    area=check.area,
                    category=check.category,
                    label=check.label,
                    outcome=state,
                    evidence_id=evidence_id,
                    source_reference=fact.source_reference if fact else None,
                    observed_at=fact.observed_at if fact else None,
                    reason=reason,
                    assessed_at=now,
                )
            )
    return tuple(result)
