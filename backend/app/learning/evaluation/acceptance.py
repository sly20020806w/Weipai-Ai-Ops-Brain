"""Replay 验收门槛；只报告证据和样本覆盖，不改变 Policy 或执行权限。"""

from pydantic import Field

from app.learning.evaluation.models import ReplayReport
from app.tools.models import ToolModel


class AcceptanceCriteria(ToolModel):
    rca_hit_rate: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    minimum_samples: int = Field(default=3, ge=1, strict=True)


class AcceptanceResult(ToolModel):
    criteria: AcceptanceCriteria
    total: int
    completed: int
    labeled: int
    hits: int
    hit_rate: float | None
    passed: bool
    reasons: tuple[str, ...]


def evaluate_benchmark(
    reports: tuple[ReplayReport, ...], criteria: AcceptanceCriteria
) -> AcceptanceResult:
    criteria = AcceptanceCriteria.model_validate_json(criteria.model_dump_json())
    reports = tuple(ReplayReport.model_validate_json(r.model_dump_json()) for r in reports)
    # 多次回放同一事故不能冒充多个基准样本。
    if len({r.task_id for r in reports}) != len(reports):
        raise ValueError("Replay 基准必须包含不同事故，不能重复计数")
    if len({r.candidate_version for r in reports}) > 1:
        raise ValueError("同一验收批次必须评估同一个候选版本")
    completed = sum(r.status == "completed" and r.candidate is not None for r in reports)
    labeled = sum(r.label_evidence_id is not None and r.candidate_hit is not None for r in reports)
    hits = sum(
        r.status == "completed"
        and r.candidate is not None
        and r.label_evidence_id is not None
        and r.candidate_hit is True
        for r in reports
    )
    rate = hits / len(reports) if reports else None
    reasons = []
    if len(reports) < criteria.minimum_samples:
        reasons.append("基准样本不足")
    if completed != len(reports):
        reasons.append("存在未完成的回放，不能排除后再计算命中率")
    if labeled != len(reports):
        reasons.append("存在未人工标注的回放")
    if rate is None or rate < criteria.rca_hit_rate:
        reasons.append("RCA 命中率未达到门槛")
    return AcceptanceResult(
        criteria=criteria,
        total=len(reports),
        completed=completed,
        labeled=labeled,
        hits=hits,
        hit_rate=rate,
        passed=not reasons,
        reasons=tuple(reasons),
    )
