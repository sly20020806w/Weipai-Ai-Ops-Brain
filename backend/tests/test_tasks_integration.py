"""本地临时 PostgreSQL 验收：真实迁移、每条合法边、原子性及并发。"""

import asyncio
import os
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC
from typing import cast
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import Table, event, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper

from app.config import Settings, parse_database_url
from app.db.session import Database
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.models import AITask, TaskServiceRequired, TaskStatusHistory
from app.tasks.service import TaskService, TaskStateConflict
from app.tasks.states import InvalidTaskTransition, TaskSource, TaskStatus, TransitionActor
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.verification_runtime import fake_verification_registry
from app.verifier.scenario import sample_spec
from app.verifier.service import VerificationService
from tests.database_support import get_test_database_url, migrate
from tests.test_tasks import EXPECTED_TRANSITIONS, LEGAL_PAIRS

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-db.ps1 进行本地 PostgreSQL 验收"
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


def path_to(target: TaskStatus) -> list[TaskStatus]:
    queue: deque[tuple[TaskStatus, list[TaskStatus]]] = deque([(TaskStatus.NEW, [])])
    visited = {TaskStatus.NEW}
    while queue:
        current, path = queue.popleft()
        if current is target:
            return path
        for value in EXPECTED_TRANSITIONS[current.value].split():
            candidate = TaskStatus(value)
            if candidate not in visited:
                visited.add(candidate)
                queue.append((candidate, [*path, candidate]))
    raise AssertionError(f"无法到达状态 {target}")


async def create_task(database: Database, source: TaskSource = TaskSource.HUMAN) -> UUID:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=source, title="payment-service 5xx 告警", reason="本地 Fake 告警"
        )
        return task.id


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(TaskSource))
async def test_task_source_uuid_and_utc_roundtrip(database: Database, source: TaskSource) -> None:
    task_id = await create_task(database, source)
    async with database.session() as session:
        task = await session.get(AITask, task_id)
        assert task is not None
        assert isinstance(task.id, UUID) and task.source is source
        assert task.status is TaskStatus.NEW and task.status_version == 0
        assert task.created_at.tzinfo is UTC and task.updated_at.tzinfo is UTC
        history = await TaskService(session).history(task_id)
        assert len(history) == 1 and history[0].from_status is None
        assert history[0].to_status is TaskStatus.NEW and history[0].sequence == 0
        assert history[0].changed_at.tzinfo is UTC and history[0].reason == "本地 Fake 告警"


@pytest.mark.asyncio
@pytest.mark.parametrize(("current", "target"), LEGAL_PAIRS)
async def test_each_legal_edge_persists_exactly_one_history(
    database: Database, current: TaskStatus, target: TaskStatus
) -> None:
    task_id = await create_task(database)
    path = [*path_to(current), target]
    async with database.session() as session, session.begin():
        service = TaskService(session)
        task = await session.get(AITask, task_id)
        assert task is not None
        for next_status in path:
            if next_status is TaskStatus.RESOLVED:
                settings = Settings(APP_ENV="test")
                snapshot = TaskSnapshot(str(task.id), task.status, task.status_version)
                async with fake_verification_registry(settings, session) as registry:
                    await VerificationService(
                        session,
                        ToolDispatcher(
                            registry, create_policy_engine(settings), LedgerService(session)
                        ),
                        settings.verification_config,
                    ).verify(snapshot, sample_spec(snapshot), reason=f"阶段进入 {next_status}")
                continue
            task = await service.transition(
                task_id,
                next_status,
                expected_status=task.status,
                expected_version=task.status_version,
                reason=f"阶段进入 {next_status}",
                actor=TransitionActor.WORKFLOW,
            )
    async with database.session() as session:
        loaded = await session.get(AITask, task_id)
        assert loaded is not None and loaded.status is target
        assert loaded.status_version == len(path) and loaded.updated_at.tzinfo is UTC
        history = await TaskService(session).history(task_id)
        assert len(history) == len(path) + 1
        assert [record.sequence for record in history] == list(range(len(history)))
        assert (history[-1].from_status, history[-1].to_status) == (current, target)
        assert history[-1].reason.startswith(f"阶段进入 {target}")
        assert all(record.changed_at.tzinfo is UTC for record in history)
        assert [record.changed_at for record in history] == sorted(
            record.changed_at for record in history
        )


@pytest.mark.asyncio
async def test_illegal_transition_and_outer_rollback_leave_no_extra_history(
    database: Database,
) -> None:
    task_id = await create_task(database)
    async with database.session() as session, session.begin():
        with pytest.raises(InvalidTaskTransition):
            await TaskService(session).transition(
                task_id,
                TaskStatus.RESOLVED,
                expected_status=TaskStatus.NEW,
                expected_version=0,
                reason="试图跳过验证",
            )
        assert len(await TaskService(session).history(task_id)) == 1
    with pytest.raises(RuntimeError, match="外层回滚"):
        async with database.session() as session, session.begin():
            await TaskService(session).transition(
                task_id,
                TaskStatus.CONTEXT_BUILDING,
                expected_status=TaskStatus.NEW,
                expected_version=0,
                reason="不应提交",
            )
            raise RuntimeError("外层回滚")
    async with database.session() as session:
        task = await session.get(AITask, task_id)
        assert task is not None and task.status is TaskStatus.NEW and task.status_version == 0
        assert len(await TaskService(session).history(task_id)) == 1


@pytest.mark.asyncio
async def test_history_insert_failure_rolls_back_status_even_if_caller_commits(
    database: Database,
) -> None:
    task_id = await create_task(database)

    def reject_history(
        mapper: Mapper[TaskStatusHistory], connection: Connection, record: TaskStatusHistory
    ) -> None:
        if record.reason == "模拟历史写入失败":
            raise RuntimeError("模拟历史写入失败")

    event.listen(TaskStatusHistory, "before_insert", reject_history)
    try:
        async with database.session() as session, session.begin():
            with pytest.raises(RuntimeError, match="模拟历史写入失败"):
                await TaskService(session).transition(
                    task_id,
                    TaskStatus.CONTEXT_BUILDING,
                    expected_status=TaskStatus.NEW,
                    expected_version=0,
                    reason="模拟历史写入失败",
                )
    finally:
        event.remove(TaskStatusHistory, "before_insert", reject_history)
    async with database.session() as session:
        task = await session.get(AITask, task_id)
        assert task is not None and task.status is TaskStatus.NEW and task.status_version == 0
        assert len(await TaskService(session).history(task_id)) == 1


@pytest.mark.asyncio
async def test_concurrent_stale_transitions_only_commit_once(database: Database) -> None:
    task_id = await create_task(database)
    ready = asyncio.Event()
    readers = 0

    async def advance() -> str:
        nonlocal readers
        try:
            async with database.session() as session, session.begin():
                # 预先加载到 identity map，再竞争行锁，验证不会使用旧缓存状态。
                task = await session.get(AITask, task_id)
                assert task is not None and task.status_version == 0
                readers += 1
                if readers == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=10)
                await TaskService(session).transition(
                    task_id,
                    TaskStatus.CONTEXT_BUILDING,
                    expected_status=TaskStatus.NEW,
                    expected_version=0,
                    reason="并发请求",
                )
            return "committed"
        except TaskStateConflict:
            return "conflict"

    outcomes = await asyncio.wait_for(asyncio.gather(advance(), advance()), timeout=20)
    assert sorted(outcomes) == ["committed", "conflict"]
    async with database.session() as session:
        task = await session.get(AITask, task_id)
        assert task is not None and task.status is TaskStatus.CONTEXT_BUILDING
        assert task.status_version == 1
        assert len(await TaskService(session).history(task_id)) == 2


@pytest.mark.asyncio
async def test_orm_and_bulk_writes_cannot_bypass_task_service(database: Database) -> None:
    task_id = await create_task(database)
    async with database.session() as session, session.begin():
        for statement in (
            update(AITask)
            .where(AITask.id == task_id)
            .values({AITask._status: TaskStatus.RESOLVED}),
            update(cast(Table, AITask.__table__))
            .where(AITask.id == task_id)
            .values(status="RESOLVED"),
            insert(TaskStatusHistory).values(task_id=task_id, reason="伪造历史"),
        ):
            with pytest.raises(TaskServiceRequired):
                await session.execute(statement)
    with pytest.raises(TaskServiceRequired):
        async with database.session() as session, session.begin():
            session.add(AITask(title="绕过服务创建", source=TaskSource.HUMAN))
    async with database.session() as session:
        task = await session.get(AITask, task_id)
        assert task is not None and task.status is TaskStatus.NEW
        assert len(await TaskService(session).history(task_id)) == 1
        assert await session.scalar(select(AITask).where(AITask.title == "绕过服务创建")) is None
