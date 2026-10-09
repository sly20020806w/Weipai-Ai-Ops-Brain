"""完整 Fake 调查/审批/执行演示；到 VERIFYING 停止，供独立 Verifier 接手。"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer

from app.agent.investigation import InvestigationSpec
from app.config import Settings
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.executor.models import ExecutionCommand, ExecutionRequest
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import ApprovalResponse, WorkflowInput


class DemoClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("Executor 演示仅允许本机临时库与 Temporal")
    settings = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        TEMPORAL_CONFIG={
            "address": address,
            "task_queue": f"executor-demo-{uuid4().hex}",
        },
    )
    database, handles = Database(url), []
    client = await Client.connect(address)
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    spec = InvestigationSpec(
        service_name="payment-service",
        title="支付回滚 Executor 验收",
        start=end - timedelta(hours=1),
        end=end,
    )
    try:
        await DiscoveryActivities(database, settings).refresh(
            DiscoveryRequest(end.isoformat(), 3600)
        )
        await TimelineActivities(database, settings).collect(
            TimelineRequest(spec.service_name, spec.start.isoformat(), spec.end.isoformat())
        )
        for decision in ("approved", "rejected", "expired"):
            connector = FakeKubernetesWriteConnector()
            executor = ExecutorActivities(database, settings, connector=connector)
            async with database.session() as session, session.begin():
                task = await TaskService(session).create(
                    source=TaskSource.ALERT, title=spec.title, reason="Step 32 Fake 演示"
                )
            async with create_worker(client, database, settings, executor_activities=executor):
                handle = await start_task_workflow(
                    client,
                    WorkflowInput(
                        str(task.id),
                        investigation_json=spec.model_dump_json(),
                        execution_enabled=True,
                        postmortem_enabled=False,
                        human_timeout_seconds=2 if decision == "expired" else 3600,
                    ),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(handle)
                async with asyncio.timeout(40):
                    while True:
                        progress = await handle.query(AITaskWorkflow.progress)
                        if progress.approval_prompt:
                            break
                        if progress.task and progress.task.status is TaskStatus.ESCALATED:
                            raise AssertionError("任务提前失败")
                        await asyncio.sleep(0.05)
                prompt = progress.approval_prompt
                assert prompt
                if decision == "approved" and interactive:
                    choice = await asyncio.to_thread(
                        input,
                        "回滚 payment-service v2.3.7→v2.3.6（L3，仅 Fake），请输入 批准 或 拒绝：",
                    )
                    if choice.strip() not in {"批准", "拒绝"}:
                        raise ValueError("只接受明确的批准或拒绝")
                    decision = "approved" if choice.strip() == "批准" else "rejected"
                if decision != "expired":
                    await handle.signal(
                        AITaskWorkflow.approve_actions,
                        ApprovalResponse(
                            prompt.task.task_id,
                            prompt.approval_id,
                            prompt.task.version,
                            prompt.action_hash,
                            decision,
                            "local-owner",
                        ),
                    )
                result = await asyncio.wait_for(handle.result(), 40)
                assert result.task and result.task.status is (
                    TaskStatus.VERIFYING if decision == "approved" else TaskStatus.ESCALATED
                )
                print(
                    f"\n审批：{decision}；状态：{result.task.status.value}；"
                    f"实际 Fake 执行次数：{connector.execution_count}",
                    flush=True,
                )
                print(f"Workflow ID：{handle.id}", flush=True)
                if decision == "approved":
                    assert connector.targets["payment-service"].image.endswith(":v2.3.6")
                    async with database.session() as session:
                        ledger = LedgerService(session)
                        record = await ledger.get_evidence(UUID(result.execution_evidence_ids[0]))
                        command = ExecutionCommand.model_validate_json(
                            json.dumps(record.parameters)
                        )
                        audits = await ledger.audits_for_task(task.id)
                        assert any(
                            a.event_type is AuditEventType.EXECUTION
                            and a.outcome == "succeeded"
                            and a.evidence_id == record.id
                            for a in audits
                        )
                        saved_task = await session.get(AITask, task.id)
                        assert saved_task and saved_task.status is TaskStatus.VERIFYING
                    # 展示外部执行成功、数据库响应丢失后的同一请求重投。
                    executing = result.history[-2]
                    request = ExecutionRequest(
                        executing, progress.action_plan_evidence_id or "", prompt
                    )
                    retried = await executor.store.execute(request)
                    assert retried.task == result.task and connector.execution_count == 1
                    clock = DemoClock()
                    scope_client = FakeKubernetesWriteConnector(clock=clock)
                    credential = await scope_client.issue(command, 60)
                    other = command.model_copy(
                        update={
                            "target": command.target.model_copy(
                                update={"service_name": "other-service"}
                            )
                        }
                    )
                    try:
                        await scope_client.execute(other, credential)
                    except PermissionError:
                        print("动作凭证操作其他服务：拒绝", flush=True)
                    else:
                        raise AssertionError("凭证越界")
                    clock.now += timedelta(seconds=60)
                    try:
                        await scope_client.execute(command, credential)
                    except PermissionError:
                        print("动作凭证到期：拒绝", flush=True)
                    else:
                        raise AssertionError("凭证未过期")
                    print(f"回滚已提交：v2.3.7 → v2.3.6；执行 Evidence ID：{record.id}", flush=True)
                    print(
                        "重投后执行次数仍为 1；状态保持 VERIFYING，等待独立业务验证。", flush=True
                    )
                    await scope_client.aclose()
                    await connector.aclose()
                    assert await executor.store.replay(request, record.id) == record.result_snapshot
                    print("关闭写客户端后的 Dispatcher Replay：通过", flush=True)
                else:
                    assert connector.execution_count == connector.issue_count == 0
                    print("未获得批准：未签发动作凭证、未调用写端。", flush=True)
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
            await connector.aclose()
        print("\nStep 32 Executor Fake 演示全部通过", flush=True)
    finally:
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 32 演示清理")
        await database.dispose()
