"""固定设计契约，穷举全部状态对并检查服务写入的历史。"""

from datetime import UTC
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.ledger.models import AuditEventType, AuditRecord
from app.tasks.models import AITask, TaskServiceRequired, TaskStatusHistory
from app.tasks.service import TaskNotFound, TaskService, TaskStateConflict
from app.tasks.states import (
    ALLOWED_TRANSITIONS,
    InvalidTaskTransition,
    TaskSource,
    TaskStatus,
    TransitionActor,
    VerificationRequired,
)

# 与 docs/task-state-machine.md 的公开契约逐项对应，独立于实现的异常出口合并逻辑。
EXPECTED_TRANSITIONS = {
    "NEW": "CONTEXT_BUILDING FAILED ESCALATED",
    "CONTEXT_BUILDING": "RUNBOOK_MATCHING WAITING_INFORMATION FAILED ESCALATED AUTOMATION_ABORTED",
    "RUNBOOK_MATCHING": "INVESTIGATING WAITING_INFORMATION FAILED ESCALATED AUTOMATION_ABORTED",
    "INVESTIGATING": "RCA NEED_HUMAN_JUDGMENT WAITING_INFORMATION "
    "FAILED ESCALATED AUTOMATION_ABORTED",
    "RCA": "PLANNING INVESTIGATING NEED_HUMAN_JUDGMENT WAITING_INFORMATION "
    "FAILED ESCALATED AUTOMATION_ABORTED",
    "PLANNING": "EXECUTING WAITING_APPROVAL NEED_HUMAN_JUDGMENT WAITING_INFORMATION "
    "INVESTIGATING FAILED ESCALATED AUTOMATION_ABORTED",
    "NEED_HUMAN_JUDGMENT": "CONTEXT_BUILDING RUNBOOK_MATCHING INVESTIGATING RCA PLANNING "
    "FAILED ESCALATED AUTOMATION_ABORTED",
    "WAITING_INFORMATION": "CONTEXT_BUILDING RUNBOOK_MATCHING INVESTIGATING RCA PLANNING "
    "FAILED ESCALATED AUTOMATION_ABORTED",
    "WAITING_APPROVAL": "EXECUTING PLANNING FAILED ESCALATED AUTOMATION_ABORTED",
    "EXECUTING": "VERIFYING INVESTIGATING FAILED ESCALATED AUTOMATION_ABORTED",
    "VERIFYING": "RESOLVED INVESTIGATING FAILED ESCALATED AUTOMATION_ABORTED",
    "RESOLVED": "LEARNING",
    "FAILED": "LEARNING ESCALATED",
    "AUTOMATION_ABORTED": "ESCALATED",
    "ESCALATED": "INVESTIGATING LEARNING",
    "LEARNING": "CLOSED FAILED ESCALATED",
    "CLOSED": "",
}
LEGAL_PAIRS = [
    (TaskStatus(current), TaskStatus(target))
    for current, targets in EXPECTED_TRANSITIONS.items()
    for target in targets.split()
]
ILLEGAL_PAIRS = [
    (current, target)
    for current in TaskStatus
    for target in TaskStatus
    if target.value not in EXPECTED_TRANSITIONS[current.value].split()
]


def loaded_task(status: TaskStatus, version: int = 0) -> AITask:
    # 穷举基础状态表用无真实结论的非关键占位任务；关键任务门禁单独集成验收。
    task = AITask(id=uuid4(), title="测试任务", source=TaskSource.HUMAN)
    # 模拟 ORM 从数据库加载已有状态，不走应用属性赋值。
    set_committed_value(task, "_status", status)
    set_committed_value(task, "_status_version", version)
    return task


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    # 拓扑测试没有持久化接管证据；真实锁存由 Step 44 数据库专项覆盖。
    monkeypatch.setattr("app.tasks.takeover.takeover_record", AsyncMock(return_value=None))
    instance = AsyncMock(spec=AsyncSession)
    instance.in_transaction.return_value = True
    return instance


def test_exact_statuses_sources_and_distinct_wait_states() -> None:
    assert {item.value for item in TaskStatus} == set(EXPECTED_TRANSITIONS)
    assert {item.value for item in TaskSource} == {
        "Alert",
        "Ticket",
        "Schedule",
        "State",
        "Prediction",
        "Release",
        "Human",
        "AI",
    }
    assert str(TaskStatus.NEED_HUMAN_JUDGMENT) != str(TaskStatus.WAITING_APPROVAL)
    assert {
        current.value: {target.value for target in targets}
        for current, targets in ALLOWED_TRANSITIONS.items()
    } == {current: set(targets.split()) for current, targets in EXPECTED_TRANSITIONS.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize(("current", "target"), LEGAL_PAIRS)
async def test_each_legal_transition_writes_one_utc_history(
    session: AsyncMock, current: TaskStatus, target: TaskStatus, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = loaded_task(current)
    session.scalar.return_value = task
    actor = TransitionActor.VERIFIER if target is TaskStatus.RESOLVED else TransitionActor.WORKFLOW
    if target is TaskStatus.RESOLVED:
        # 本测试穷举拓扑；真实验证证据门禁由 Step 31 PostgreSQL 专项独立覆盖。
        monkeypatch.setattr("app.verifier.authority.require_verification", AsyncMock())
    if target is TaskStatus.EXECUTING:
        # 本测试穷举拓扑；持久化熔断门禁由 Step 33 PostgreSQL 专项覆盖。
        monkeypatch.setattr("app.tasks.safety.service.require_automation_active", AsyncMock())
    result = await TaskService(session).transition(
        task.id, target, expected_status=current, expected_version=0, reason="阶段完成", actor=actor
    )
    assert result.status is target and result.status_version == 1
    assert session.add.call_count == 2
    history = session.add.call_args_list[0].args[0]
    assert isinstance(history, TaskStatusHistory)
    assert (history.task_id, history.from_status, history.to_status) == (task.id, current, target)
    assert history.sequence == 1 and history.reason == "阶段完成"
    assert history.changed_at.tzinfo is UTC and history.actor is actor
    audit = session.add.call_args_list[1].args[0]
    assert isinstance(audit, AuditRecord) and audit.task_id == task.id
    assert audit.event_type is AuditEventType.STATE_TRANSITION and audit.actor == actor.value
    assert audit.occurred_at == history.changed_at
    assert audit.details == {
        "from_status": current.value,
        "to_status": target.value,
        "status_version": 1,
        "reason": "阶段完成",
    }
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("current", "target"), ILLEGAL_PAIRS)
async def test_each_illegal_transition_preserves_state_without_history(
    session: AsyncMock, current: TaskStatus, target: TaskStatus
) -> None:
    task = loaded_task(current)
    session.scalar.return_value = task
    with pytest.raises(InvalidTaskTransition):
        await TaskService(session).transition(
            task.id, target, expected_status=current, expected_version=0, reason="非法迁移"
        )
    assert task.status is current and task.status_version == 0
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(TaskSource))
async def test_create_starts_new_and_records_reason(session: AsyncMock, source: TaskSource) -> None:
    task = await TaskService(session).create(source=source, title="  新任务  ", reason="告警接入")
    assert task.source is source and task.status is TaskStatus.NEW and task.status_version == 0
    assert task.title == "新任务"
    assert session.add.call_count == 3
    history = session.add.call_args_list[1].args[0]
    assert history.from_status is None and history.to_status is TaskStatus.NEW
    assert history.sequence == 0 and history.task_id == task.id
    assert history.reason == "告警接入" and history.changed_at.tzinfo is UTC
    audit = session.add.call_args_list[2].args[0]
    assert isinstance(audit, AuditRecord) and audit.operation == "task.create"
    assert audit.occurred_at == history.changed_at


@pytest.mark.asyncio
async def test_resolution_requires_verifier(session: AsyncMock) -> None:
    task = loaded_task(TaskStatus.VERIFYING)
    session.scalar.return_value = task
    with pytest.raises(VerificationRequired):
        await TaskService(session).transition(
            task.id,
            TaskStatus.RESOLVED,
            expected_status=TaskStatus.VERIFYING,
            expected_version=0,
            reason="执行返回成功",
        )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_missing_task_and_conflicting_version(session: AsyncMock) -> None:
    service = TaskService(session)
    session.scalar.return_value = None
    with pytest.raises(TaskNotFound):
        await service.transition(
            uuid4(),
            TaskStatus.CONTEXT_BUILDING,
            expected_status=TaskStatus.NEW,
            expected_version=0,
            reason="启动",
        )
    task = loaded_task(TaskStatus.NEW, 2)
    session.scalar.return_value = task
    with pytest.raises(TaskStateConflict):
        await service.transition(
            task.id,
            TaskStatus.CONTEXT_BUILDING,
            expected_status=TaskStatus.NEW,
            expected_version=0,
            reason="旧请求",
        )
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_transaction_and_nonblank_fields_required(session: AsyncMock) -> None:
    service = TaskService(session)
    session.in_transaction.return_value = False
    with pytest.raises(RuntimeError, match="开启事务"):
        await service.create(source=TaskSource.HUMAN, title="任务", reason="创建")
    session.in_transaction.return_value = True
    for title, reason in [(" ", "创建"), ("x" * 501, "创建"), ("任务", " \n")]:
        with pytest.raises(ValueError):
            await service.create(source=TaskSource.HUMAN, title=title, reason=reason)
    with pytest.raises(ValueError, match="reason"):
        await service.transition(
            uuid4(),
            TaskStatus.CONTEXT_BUILDING,
            expected_status=TaskStatus.NEW,
            expected_version=0,
            reason=" \n",
        )
    session.add.assert_not_called()


def test_status_assignment_outside_service_is_rejected() -> None:
    task = loaded_task(TaskStatus.NEW)
    with pytest.raises(AttributeError):
        task.status = TaskStatus.CONTEXT_BUILDING  # type: ignore[misc]
    with pytest.raises(TaskServiceRequired):
        task._status = TaskStatus.CONTEXT_BUILDING
    with pytest.raises(TaskServiceRequired):
        task._status_version = 1
