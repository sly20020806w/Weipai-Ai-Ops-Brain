"""在隔离库运行真实 Temporal + Fake 工单，供人自行验收。"""

import asyncio
import json
import os
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.ops_platform.tickets import TicketState
from app.db.base import utc_now
from app.db.session import Database
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.tickets.activities import TicketActivities
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import ApprovalResponse, HumanAnswer, WorkflowProgress
from app.triggers.activities import EventActivities
from app.triggers.schemas import EventReceipt, NormalizedEvent
from app.triggers.service import EventService


def demo_settings(queue: str, *, timeout: float = 120) -> Settings:
    return Settings(
        APP_ENV="test",
        TICKET_CONFIG={
            "enabled": True,
            "bindings": [
                {
                    "service_name": "payment-service",
                    "requester_id": "requester-sample",
                    "subject_id": "user-sample",
                    "resource": "payment-logs",
                    "permission": "read",
                }
            ],
        },
        EXECUTION_CONFIG={"enabled": True},
        TEMPORAL_CONFIG={
            "task_queue": queue,
            "human_timeout_seconds": timeout,
            "activity_timeout_seconds": 30,
            "activity_max_attempts": 1,
        },
    )


async def push_ticket(
    database: Database, state: TicketState, *, complete: bool = True
) -> EventReceipt:
    original = state.tickets["TICKET-PERMISSION" if complete else "TICKET-INCOMPLETE"]
    ticket_id = "ticket-" + uuid4().hex
    state.tickets[ticket_id] = original.model_copy(update={"id": ticket_id})
    async with database.session() as session, session.begin():
        return (
            await EventService(session).accept(
                [
                    NormalizedEvent(
                        origin="ops_platform",
                        source=TaskSource.TICKET,
                        external_id=ticket_id,
                        service_name=original.service_name,
                        title=original.title,
                        occurred_at=original.created_at,
                    )
                ]
            )
        )[0]


async def wait_progress(
    handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress], status: TaskStatus
) -> WorkflowProgress:
    async with asyncio.timeout(60):
        while True:
            progress = await handle.query(AITaskWorkflow.progress)
            if (
                progress.task
                and progress.task.status is status
                and (status is not TaskStatus.WAITING_APPROVAL or progress.approval_prompt)
                and (
                    status not in {TaskStatus.WAITING_INFORMATION, TaskStatus.NEED_HUMAN_JUDGMENT}
                    or progress.human_prompt
                )
            ):
                return progress
            if progress.task and progress.task.status in {
                TaskStatus.ESCALATED,
                TaskStatus.FAILED,
                TaskStatus.AUTOMATION_ABORTED,
            }:
                raise AssertionError(f"工单提前停止：{progress}")
            await asyncio.sleep(0.05)


async def approve(
    handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress], *, decision: str = "approved"
) -> None:
    progress = await wait_progress(handle, TaskStatus.WAITING_APPROVAL)
    prompt = progress.approval_prompt
    assert prompt is not None
    await handle.signal(
        AITaskWorkflow.approve_actions,
        ApprovalResponse(
            prompt.task.task_id,
            prompt.approval_id,
            prompt.task.version,
            prompt.action_hash,
            decision,
            "demo-operator",
        ),
    )


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("工单演示只允许本机隔离临时库和 Temporal")
    database = Database(url)
    client = await Client.connect(
        address, namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default")
    )
    queue = "ticket-demo-" + uuid4().hex
    settings = demo_settings(queue)
    state = TicketState()
    tickets = TicketActivities(database, settings, state=state)
    handles: list[WorkflowHandle[AITaskWorkflow, WorkflowProgress]] = []
    try:
        async with create_worker(client, database, settings, ticket_activities=tickets):
            for complete in (True, False):
                # 每个独立源工单在独立 Fake 资源权限范围内演示。
                state.grants.clear()
                receipt = await push_ticket(database, state, complete=complete)
                await EventActivities(database, settings, client).start_task(receipt)
                handle = client.get_workflow_handle(
                    receipt.workflow_id, result_type=WorkflowProgress
                )
                handles.append(handle)
                if not complete:
                    progress = await wait_progress(handle, TaskStatus.WAITING_INFORMATION)
                    prompt = progress.human_prompt
                    assert prompt is not None
                    print("信息不全工单状态：WAITING_INFORMATION；实际写入 0（本工单）", flush=True)
                    answer = json.dumps(
                        {
                            "resource": "payment-logs",
                            "permission": "read",
                            "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
                            "reason": "排查支付工单",
                        },
                        ensure_ascii=False,
                    )
                    if interactive:
                        print("请输入补充信息 JSON（直接回车使用以下样例）：" + answer, flush=True)
                        answer = await asyncio.to_thread(input) or answer
                    await handle.signal(
                        AITaskWorkflow.answer_question,
                        HumanAnswer(
                            prompt.question_id,
                            prompt.task.status,
                            prompt.task.version,
                            answer,
                            "demo-operator",
                        ),
                    )
                progress = await wait_progress(handle, TaskStatus.WAITING_APPROVAL)
                print(
                    "待审批动作：授予 user-sample / payment-logs / read（L4），"
                    "独立验证后回填关闭（L1）",
                    flush=True,
                )
                print("动作计划：" + str(progress.action_plan_json), flush=True)
                decision = "approved"
                if interactive:
                    print("请输入“批准”或“拒绝”：", flush=True)
                    decision = (
                        "approved" if await asyncio.to_thread(input) == "批准" else "rejected"
                    )
                before = state.execution_count
                await approve(handle, decision=decision)
                result = await asyncio.wait_for(handle.result(), timeout=60)
                assert result.task and result.task.status is (
                    TaskStatus.CLOSED if decision == "approved" else TaskStatus.ESCALATED
                )
                # 源工单 ID 从 OpsEvent 读回，避免演示选择相同时间的旧工单。
                from uuid import UUID

                from app.triggers.models import OpsEvent

                async with database.session() as session:
                    event = await session.get(OpsEvent, UUID(receipt.event_id))
                    assert event is not None
                    ticket_id = event.external_id
                print(
                    f"工单状态：{state.tickets[ticket_id].status}；"
                    f"任务状态：{result.task.status.value}；"
                    f"本工单写入：{state.execution_count - before}",
                    flush=True,
                )
                if decision == "approved":
                    assert (
                        state.execution_count - before == 2 and state.tickets[ticket_id].resolution
                    )
                    print(state.tickets[ticket_id].resolution, flush=True)
                    print(
                        f"验证 Evidence：{result.verification_evidence_id}；"
                        f"学习 Evidence：{result.postmortem_evidence_id}",
                        flush=True,
                    )
                else:
                    assert state.execution_count == before
                print("Temporal Workflow：" + receipt.workflow_id, flush=True)
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
        print("Step 38 工单场景 Fake 演示全部通过", flush=True)
    finally:
        for handle in handles:
            description = await handle.describe()
            if description.close_time is None:
                await handle.terminate("工单演示清理")
        await database.dispose()
