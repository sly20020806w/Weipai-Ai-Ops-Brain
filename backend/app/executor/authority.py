"""仅宿主 Executor 在验收真实计划/审批后建立的调用范围；Agent 入参不能赋权。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from app.policy.models import PolicyResult, RunbookPolicyContext
from app.tools.models import JsonObject


@dataclass(frozen=True)
class _ExecutionAuthority:
    task_id: UUID
    parameters: JsonObject
    policy: PolicyResult
    session_id: int
    runbook: RunbookPolicyContext | None = None


_scope: ContextVar[_ExecutionAuthority | None] = ContextVar("executor_authority", default=None)


@contextmanager
def _execution_scope(value: _ExecutionAuthority) -> Iterator[None]:
    token = _scope.set(value)
    try:
        yield
    finally:
        _scope.reset(token)


def execution_authority(
    task_id: UUID, parameters: JsonObject, session_id: int
) -> _ExecutionAuthority | None:
    value = _scope.get()
    if value is not None and (
        value.task_id == task_id
        and value.parameters == parameters
        and value.session_id == session_id
    ):
        return value
    return None


def execution_active(task_id: UUID, session_id: int) -> bool:
    value = _scope.get()
    return value is not None and value.task_id == task_id and value.session_id == session_id
