"""临时数据库与隔离 Worker 中的重大活动保障演示。"""

import asyncio
import json
import os
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.war_room.facts import FakeWarRoomConnector
from app.db.base import utc_now
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.ledger.service import LedgerService
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus
from app.tasks.war_room.activities import WarRoomActivities
from app.tasks.war_room.models import SECTIONS, WarRoomAssessment, WarRoomSubmission
from app.tasks.war_room.scenario import fake_resources, seed_runbook
from app.tasks.war_room.service import submit_war_room
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import ApprovalResponse, WorkflowProgress
from app.triggers.activities import EventActivities


async def drive(
    handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress],
    *,
    decision: str = "approved",
    interactive: bool = False,
) -> WorkflowProgress:
    # 仅验收驱动器观察进度并模拟本人信号；实际生命周期全部由 Temporal 编排。
    result = asyncio.create_task(handle.result())
    seen: set[str] = set()
    try:
        async with asyncio.timeout(180):
            while not result.done():
                progress = await handle.query(AITaskWorkflow.progress)
                prompt = progress.approval_prompt
                if (
                    prompt is not None
                    and progress.task == prompt.task
                    and prompt.approval_id not in seen
                ):
                    seen.add(prompt.approval_id)
                    chosen = decision
                    if interactive:
                        print("\n需要审批：" + (progress.action_plan_json or ""), flush=True)
                        answer = await asyncio.to_thread(input, "输入 approve 批准或 reject 拒绝：")
                        chosen = "approved" if answer.strip().lower() == "approve" else "rejected"
                    await handle.signal(
                        AITaskWorkflow.approve_actions,
                        ApprovalResponse(
                            prompt.task.task_id,
                            prompt.approval_id,
                            prompt.task.version,
                            prompt.action_hash,
                            chosen,
                            "fake-owner",
                        ),
                    )
                await asyncio.sleep(0.1)
            return await result
    finally:
        if not result.done():
            result.cancel()
            await asyncio.gather(result, return_exceptions=True)


async def stop_anomalies(client: Client, database: Database, task_id: UUID) -> None:
    async with database.session() as session:
        ids = {
            str(r.task_id)
            for e in await LedgerService(session).evidence_for_task(task_id)
            if e.source_tool == "war_room.assessment"
            for r in WarRoomAssessment.model_validate_json(json.dumps(e.result_snapshot)).receipts
        }
    for child_id in ids:
        handle = client.get_workflow_handle("ai-task-" + child_id)
        if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate("清理本机 Fake 保障异常处置演示")


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("重大保障演示只允许本机临时库和 Temporal")
    database = Database(url)
    client = await Client.connect(
        address, namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default")
    )
    resources, binding = fake_resources()
    facts = FakeWarRoomConnector(abnormal_windows=frozenset({0, 1}))
    settings = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True, "bindings": (binding,)},
        TEMPORAL_CONFIG={
            "address": address,
            "task_queue": "war-room-demo-" + uuid4().hex,
            "human_timeout_seconds": 120,
        },
        WAR_ROOM_CONFIG={"interval_seconds": 30.0 if interactive else 1.0, "max_windows": 120},
    )
    receipt = None
    try:
        await seed_runbook(database, binding.service_name)
        now = utc_now()
        value = WarRoomSubmission(
            request_id=uuid4(),
            service_name=binding.service_name,
            title="支付高峰活动",
            kind="event",
            start=now + timedelta(seconds=30 if interactive else 2),
            end=now + timedelta(seconds=90 if interactive else 10),
            projected_rps=1000.0,
        )
        async with database.session() as session, session.begin():
            receipt = await submit_war_room(session, value)
            repeated = await submit_war_room(session, value)
            assert repeated.duplicate and repeated.task_id == receipt.task_id
        async with create_worker(
            client,
            database,
            settings,
            executor_activities=ExecutorActivities(database, settings, connector=resources),
            war_room_activities=WarRoomActivities(
                database, settings, resources=resources, facts=facts
            ),
        ):
            await EventActivities(database, settings, client).start_task(receipt)
            handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
            try:
                progress = await drive(handle, interactive=interactive)
            finally:
                if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
                    await handle.terminate("清理本机 Fake 保障主任务")
            if progress.task is None or progress.task.status != TaskStatus.CLOSED:
                print(
                    f"保障转人工：{progress.task}；资源动作数 {resources.execution_count}",
                    flush=True,
                )
                if interactive:
                    return
                raise AssertionError("保障演示未闭环")
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
        async with database.session() as session:
            ledger = LedgerService(session)
            records = await ledger.evidence_for_task(UUID(receipt.task_id))
            report = await ledger.get_evidence(UUID(progress.postmortem_evidence_id or ""))
            data = json.loads(json.dumps(report.result_snapshot))
            assert tuple(s["name"] for s in data["sections"]) == SECTIONS
            assert len(data["anomaly_tasks"]) == 1 and len(data["executions"]) == 2
            for section in data["sections"]:
                assert section["evidence_ids"]
                for evidence_id in section["evidence_ids"]:
                    assert (await ledger.get_evidence(UUID(evidence_id))).task_id == UUID(
                        receipt.task_id
                    )
            history = await TaskService(session).history(UUID(receipt.task_id))
            assert [h.to_status for h in history] == [h.status for h in progress.history]
            preparations = [
                e
                for e in records
                if e.source_tool == "war_room.assessment"
                and e.parameters.get("purpose") == "prepare"
            ]
            print("保障准备检查清单与容量评估：", flush=True)
            checklist = WarRoomAssessment.model_validate_json(
                json.dumps(preparations[0].result_snapshot)
            )
            print(
                f"预计峰值 {value.projected_rps:g} RPS，"
                f"原副本 {checklist.baseline.replicas}，所需副本 {checklist.required_replicas}；"
                f"Evidence {preparations[0].id}",
                flush=True,
            )
            for item in checklist.checks:
                print(f"  {item.label}：{item.outcome}；Evidence {item.evidence_id}", flush=True)
            print("保障报告：", flush=True)
            for section in data["sections"]:
                print(f"  {section['name']}：{section['statement']}", flush=True)
                print("    Evidence：" + "、".join(section["evidence_ids"]), flush=True)
        assert (
            resources.execution_count == 2 and resources.targets[binding.service_name].replicas == 3
        )
        print(
            json.dumps(
                {
                    "任务状态": "CLOSED",
                    "报告章节": 11,
                    "容量准备": "3 → 5 副本",
                    "保障结束回收": "5 → 3 副本",
                    "异常处置任务": 1,
                    "分别审批的 Fake 动作": 2,
                    "重复提交新增任务": 0,
                    "Workflow ID": receipt.workflow_id,
                    "报告 Evidence ID": str(report.id),
                    "Temporal 历史回放": "通过",
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        print("Step 42 War Room Fake 演示全部通过", flush=True)
    finally:
        if receipt is not None:
            await stop_anomalies(client, database, UUID(receipt.task_id))
        await resources.aclose()
        await database.dispose()
