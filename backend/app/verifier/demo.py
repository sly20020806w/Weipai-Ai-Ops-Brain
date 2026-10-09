"""本机 Fake 独立验证演示；准备 VERIFYING 任务，不执行回滚或其他运维写操作。"""

import asyncio
import os
from functools import partial
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client

from app.config import Settings
from app.db.session import Database
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus, TransitionActor, VerificationRequired
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow_models import TaskSnapshot, WorkflowInput
from app.tools.verification_runtime import fake_verification_registry
from app.verifier.activities import VerifierActivities
from app.verifier.models import VerificationReport
from app.verifier.scenario import sample_spec


async def prepare(database: Database) -> TaskSnapshot:
    async with database.session() as session, session.begin():
        service = TaskService(session)
        task = await service.create(
            source=TaskSource.HUMAN,
            title="Step 31 验证样例",
            reason="Fake 前置任务，不执行运维动作",
        )
        for target in (
            TaskStatus.CONTEXT_BUILDING,
            TaskStatus.RUNBOOK_MATCHING,
            TaskStatus.INVESTIGATING,
            TaskStatus.RCA,
            TaskStatus.PLANNING,
            TaskStatus.EXECUTING,
            TaskStatus.VERIFYING,
        ):
            task = await service.transition(
                task.id,
                target,
                expected_status=task.status,
                expected_version=task.status_version,
                reason="Fake 样例前置阶段",
            )
        return TaskSnapshot(str(task.id), task.status, task.status_version)


async def run_demo(url: URL) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS")
    if not address or address.rpartition(":")[0] != "127.0.0.1":
        raise ValueError("演示只允许本机 Temporal")
    database = Database(url)
    handles = []
    try:
        client = await Client.connect(
            address, namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default")
        )
        for recovered in (True, False):
            snapshot = await prepare(database)
            async with database.session() as session, session.begin():
                try:
                    await TaskService(session).transition(
                        UUID(snapshot.task_id),
                        TaskStatus.RESOLVED,
                        expected_status=TaskStatus.VERIFYING,
                        expected_version=snapshot.version,
                        reason="外部代码试图跳过验证",
                        actor=TransitionActor.VERIFIER,
                    )
                except VerificationRequired:
                    print("伪造 actor=verifier 设置 RESOLVED：已拒绝", flush=True)
                else:
                    raise AssertionError("外部调用绕过了独立 Verifier")
            settings = Settings(
                APP_ENV="test",
                TEMPORAL_CONFIG={
                    "address": address,
                    "namespace": client.namespace,
                    "task_queue": f"verifier-demo-{uuid4().hex}",
                },
            )
            activities = VerifierActivities(
                database,
                settings,
                registry_factory=partial(fake_verification_registry, recovered=recovered),
            )
            async with create_worker(client, database, settings, verifier_activities=activities):
                handle = await start_task_workflow(
                    client,
                    WorkflowInput(
                        snapshot.task_id,
                        verification_json=sample_spec(snapshot).model_dump_json(),
                        postmortem_enabled=False,
                    ),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(handle)
                result = await asyncio.wait_for(handle.result(), timeout=30)
            assert result.task and result.verification_json and result.verification_evidence_id
            report = VerificationReport.model_validate_json(result.verification_json)
            assert result.task.status is (
                TaskStatus.RESOLVED if recovered else TaskStatus.INVESTIGATING
            )
            async with database.session() as session:
                task = await session.get(AITask, UUID(snapshot.task_id))
                assert task and task.status is result.task.status
                evidence = await LedgerService(session).evidence_for_task(task.id)
                assert len(evidence) == 9
            print(
                f"场景：{'指标恢复' if recovered else '指标未恢复'}；"
                f"最终状态：{result.task.status}",
                flush=True,
            )
            print(
                f"Workflow ID：{handle.id}；聚合 Evidence ID：{result.verification_evidence_id}",
                flush=True,
            )
            for check in report.checks:
                print(
                    f"  {check.name}：{'通过' if check.passed else '未通过'}；"
                    f"Evidence ID：{check.evidence_id}",
                    flush=True,
                )
        print("实际运维动作执行次数：0", flush=True)
        print("Step 31 Verifier Fake 演示全部通过", flush=True)
    finally:
        for handle in handles:
            description = await handle.describe()
            if description.status is not None and description.status.name == "RUNNING":
                await handle.terminate("Step 31 演示清理")
        await database.dispose()
