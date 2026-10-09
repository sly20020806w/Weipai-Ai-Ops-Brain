"""Step 17：真实数据库上的 Activity 幂等性、并发与 Verifier 边界。"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio
from temporalio.exceptions import ApplicationError

from app.config import parse_database_url
from app.db.session import Database
from app.ledger.service import LedgerService
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.service import TaskService, TaskStateConflict
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest
from app.verifier.placeholder import PlaceholderVerifier
from tests.database_support import get_test_database_url, migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-db.ps1 或 check-workflow.ps1"
)


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        yield instance
    finally:
        await instance.dispose()


async def create_task(database: Database) -> TaskSnapshot:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="Workflow 占位验收", reason="独立临时测试库"
        )
        return TaskSnapshot(str(task.id), task.status, task.status_version)


@pytest.mark.asyncio
async def test_concurrent_retry_writes_one_history_and_audit(database: Database) -> None:
    task = await create_task(database)
    store = TaskActivityStore(database)
    request = TransitionRequest(task, TaskStatus.CONTEXT_BUILDING, "同一 Activity 请求")
    first, second = await asyncio.gather(store.transition(request), store.transition(request))
    assert first == second and first.version == 1
    async with database.session() as session:
        from uuid import UUID

        assert len(await TaskService(session).history(UUID(task.task_id))) == 2
        assert len(await LedgerService(session).audits_for_task(UUID(task.task_id))) == 2
    # 更晚阶段提交后，旧 Activity 重试仍返回它原来的结果。
    await store.transition(TransitionRequest(first, TaskStatus.RUNBOOK_MATCHING, "下一阶段"))
    assert await store.transition(request) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reason", "target", "source"])
async def test_retry_cannot_reuse_sequence_for_different_request(
    database: Database, change: str
) -> None:
    store = TaskActivityStore(database)
    task = await create_task(database)
    request = TransitionRequest(task, TaskStatus.CONTEXT_BUILDING, "原请求")
    await store.transition(request)
    modified = {
        "reason": replace(request, reason="不同原因"),
        "target": replace(request, target=TaskStatus.FAILED),
        "source": replace(request, task=replace(task, status=TaskStatus.INVESTIGATING)),
    }[change]
    with pytest.raises(TaskStateConflict):
        await store.transition(modified)


@pytest.mark.asyncio
async def test_workflow_actor_cannot_resolve_and_independent_verifier_can(
    database: Database,
) -> None:
    from uuid import UUID

    from tests.test_tasks_integration import path_to

    task = await create_task(database)
    store = TaskActivityStore(database)
    for target in path_to(TaskStatus.VERIFYING):
        task = await store.transition(TransitionRequest(task, target, "占位前置阶段"))
    request = TransitionRequest(task, TaskStatus.RESOLVED, "占位验证通过")
    with pytest.raises(ApplicationError, match="被拒绝"):
        await TaskActivities(store).transition(request)
    resolved = await PlaceholderVerifier(store, app_env="test").verify(request)
    assert resolved.status is TaskStatus.RESOLVED
    assert await PlaceholderVerifier(store, app_env="test").verify(request) == resolved
    async with database.session() as session:
        history = await TaskService(session).history(UUID(task.task_id))
        assert history[-1].actor.value == "verifier"
    with pytest.raises(ApplicationError):
        await PlaceholderVerifier(store, app_env="test").verify(
            replace(request, target=TaskStatus.CLOSED)
        )


@pytest.mark.asyncio
async def test_new_workflow_rejects_already_started_task(database: Database) -> None:
    task = await create_task(database)
    store = TaskActivityStore(database)
    await store.transition(TransitionRequest(task, TaskStatus.CONTEXT_BUILDING, "已开始"))
    with pytest.raises(ApplicationError) as error:
        await TaskActivities(store).load(task.task_id)
    assert error.value.non_retryable
