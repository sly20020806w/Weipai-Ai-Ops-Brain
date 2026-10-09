"""成熟度纯计算规则；审核和验证证据由宿主服务验收，配置不落库。"""

import hashlib
import json
from uuid import UUID

from pydantic import Field, model_validator

from app.runbooks.schemas import AutomationLevel, RunbookMaturity, RunbookView
from app.tools.models import ToolModel


class MaturityConfig(ToolModel):
    verified_successes: int = Field(default=3, ge=1)
    semi_automated_successes: int = Field(default=5, ge=1)
    approval_automated_successes: int = Field(default=10, ge=1)
    self_healing_successes: int = Field(default=20, ge=1)
    consecutive_failure_limit: int = Field(default=2, ge=1)
    min_confidence: float = Field(default=0.8, gt=0, le=1, allow_inf_nan=False)
    self_healing_confidence: float = Field(default=0.95, gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self) -> "MaturityConfig":
        counts = (
            self.verified_successes,
            self.semi_automated_successes,
            self.approval_automated_successes,
            self.self_healing_successes,
        )
        if any(left >= right for left, right in zip(counts, counts[1:], strict=False)):
            raise ValueError("Runbook 晋级成功次数阈值必须严格递增")
        if self.self_healing_confidence < self.min_confidence:
            raise ValueError("Self-Healing 可信度不能低于普通晋级门槛")
        return self


class MaturityState(ToolModel):
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    maturity: RunbookMaturity = RunbookMaturity.DRAFT
    review_evidence_id: UUID | None = None


def content_hash(runbook: RunbookView) -> str:
    data = runbook.model_dump(
        mode="json",
        exclude={
            "success_count",
            "failure_count",
            "confidence",
            "maturity",
            "automation_level",
            "created_at",
            "updated_at",
            "embedding_model",
            "embedding_dimensions",
        },
    )
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def config_hash(config: MaturityConfig) -> str:
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


def automation_level(maturity: RunbookMaturity) -> AutomationLevel:
    return {
        RunbookMaturity.DRAFT: AutomationLevel.MANUAL,
        RunbookMaturity.REVIEWED: AutomationLevel.MANUAL,
        RunbookMaturity.VERIFIED: AutomationLevel.MANUAL,
        RunbookMaturity.SEMI_AUTOMATED: AutomationLevel.SEMI_AUTOMATED,
        RunbookMaturity.APPROVAL_AUTOMATED: AutomationLevel.APPROVAL_AUTOMATED,
        RunbookMaturity.SELF_HEALING: AutomationLevel.SELF_HEALING,
    }[maturity]


def record_result(state: MaturityState, passed: bool, config: MaturityConfig) -> MaturityState:
    state = MaturityState.model_validate(state)
    if type(passed) is not bool:
        raise TypeError("验证结果必须是布尔值")
    success = state.success_count + int(passed)
    failure = state.failure_count + int(not passed)
    consecutive = 0 if passed else state.consecutive_failures + 1
    confidence = (success + 1) / (success + failure + 2)
    levels = list(RunbookMaturity)
    index = levels.index(state.maturity)
    if state.review_evidence_id is None:
        index = 0
    elif not passed:
        # 首次失败立即撤回自愈；连续失败退回 Reviewed 并撤销审核，必须重新审核。
        if consecutive >= config.consecutive_failure_limit:
            index = 1
        elif index == 5:
            index = 4
        if confidence < config.min_confidence:
            index = min(index, 1)
    elif index in {1, 2, 3, 4}:
        threshold = (
            config.verified_successes,
            config.semi_automated_successes,
            config.approval_automated_successes,
            config.self_healing_successes,
        )[index - 1]
        minimum = config.self_healing_confidence if index == 4 else config.min_confidence
        if success >= threshold and confidence >= minimum:
            index += 1
    return MaturityState(
        success_count=success,
        failure_count=failure,
        consecutive_failures=consecutive,
        confidence=confidence,
        maturity=levels[index],
        review_evidence_id=(
            None if consecutive >= config.consecutive_failure_limit else state.review_evidence_id
        ),
    )
