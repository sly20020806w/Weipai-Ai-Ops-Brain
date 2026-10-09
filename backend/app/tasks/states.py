"""SPEC 全部任务来源、状态和允许的阶段迁移；不负责调度或重试。"""

from enum import StrEnum
from types import MappingProxyType
from typing import Final


class TaskSource(StrEnum):
    ALERT = "Alert"
    TICKET = "Ticket"
    SCHEDULE = "Schedule"
    STATE = "State"
    PREDICTION = "Prediction"
    RELEASE = "Release"
    HUMAN = "Human"
    AI = "AI"


class TaskStatus(StrEnum):
    NEW = "NEW"
    CONTEXT_BUILDING = "CONTEXT_BUILDING"
    RUNBOOK_MATCHING = "RUNBOOK_MATCHING"
    INVESTIGATING = "INVESTIGATING"
    RCA = "RCA"
    PLANNING = "PLANNING"
    NEED_HUMAN_JUDGMENT = "NEED_HUMAN_JUDGMENT"
    WAITING_INFORMATION = "WAITING_INFORMATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    AUTOMATION_ABORTED = "AUTOMATION_ABORTED"
    ESCALATED = "ESCALATED"
    LEARNING = "LEARNING"
    CLOSED = "CLOSED"


class TransitionActor(StrEnum):
    WORKFLOW = "workflow"
    VERIFIER = "verifier"


class InvalidTaskTransition(ValueError):
    pass


class VerificationRequired(InvalidTaskTransition):
    pass


# 调查/执行阶段的异常出口；具体触发条件由后续 Workflow 与熔断器负责。
_INTERRUPTIONS = frozenset({TaskStatus.FAILED, TaskStatus.ESCALATED, TaskStatus.AUTOMATION_ABORTED})
_RESUME_PHASES = frozenset(
    {
        TaskStatus.CONTEXT_BUILDING,
        TaskStatus.RUNBOOK_MATCHING,
        TaskStatus.INVESTIGATING,
        TaskStatus.RCA,
        TaskStatus.PLANNING,
    }
)

ALLOWED_TRANSITIONS: Final = MappingProxyType(
    {
        TaskStatus.NEW: frozenset(
            {TaskStatus.CONTEXT_BUILDING, TaskStatus.FAILED, TaskStatus.ESCALATED}
        ),
        TaskStatus.CONTEXT_BUILDING: _INTERRUPTIONS
        | {TaskStatus.RUNBOOK_MATCHING, TaskStatus.WAITING_INFORMATION},
        TaskStatus.RUNBOOK_MATCHING: _INTERRUPTIONS
        | {TaskStatus.INVESTIGATING, TaskStatus.WAITING_INFORMATION},
        TaskStatus.INVESTIGATING: _INTERRUPTIONS
        | {TaskStatus.RCA, TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION},
        TaskStatus.RCA: _INTERRUPTIONS
        | {
            TaskStatus.PLANNING,
            TaskStatus.INVESTIGATING,
            TaskStatus.NEED_HUMAN_JUDGMENT,
            TaskStatus.WAITING_INFORMATION,
        },
        TaskStatus.PLANNING: _INTERRUPTIONS
        | {
            TaskStatus.EXECUTING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.NEED_HUMAN_JUDGMENT,
            TaskStatus.WAITING_INFORMATION,
            TaskStatus.INVESTIGATING,
        },
        TaskStatus.NEED_HUMAN_JUDGMENT: _INTERRUPTIONS | _RESUME_PHASES,
        TaskStatus.WAITING_INFORMATION: _INTERRUPTIONS | _RESUME_PHASES,
        TaskStatus.WAITING_APPROVAL: _INTERRUPTIONS | {TaskStatus.EXECUTING, TaskStatus.PLANNING},
        TaskStatus.EXECUTING: _INTERRUPTIONS | {TaskStatus.VERIFYING, TaskStatus.INVESTIGATING},
        TaskStatus.VERIFYING: _INTERRUPTIONS | {TaskStatus.RESOLVED, TaskStatus.INVESTIGATING},
        TaskStatus.RESOLVED: frozenset({TaskStatus.LEARNING}),
        TaskStatus.FAILED: frozenset({TaskStatus.LEARNING, TaskStatus.ESCALATED}),
        TaskStatus.AUTOMATION_ABORTED: frozenset({TaskStatus.ESCALATED}),
        TaskStatus.ESCALATED: frozenset({TaskStatus.INVESTIGATING, TaskStatus.LEARNING}),
        TaskStatus.LEARNING: frozenset(
            {TaskStatus.CLOSED, TaskStatus.FAILED, TaskStatus.ESCALATED}
        ),
        TaskStatus.CLOSED: frozenset(),
    }
)


def validate_transition(
    current: TaskStatus,
    target: TaskStatus,
    *,
    actor: TransitionActor = TransitionActor.WORKFLOW,
) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTaskTransition(f"不允许任务状态迁移：{current.value} → {target.value}")
    if target is TaskStatus.RESOLVED and actor is not TransitionActor.VERIFIER:
        raise VerificationRequired("RESOLVED 只能由独立 Verifier 经 tasks 服务设置")
