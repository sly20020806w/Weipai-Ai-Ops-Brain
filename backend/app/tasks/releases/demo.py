"""隔离本机 PostgreSQL/Temporal 下的发布验收，全部源系统使用 Fake。"""

import asyncio
import json
import os
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.changes.releases import FakeReleaseState
from app.db.base import utc_now
from app.db.session import Database
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.service import LedgerService
from app.tasks.planning.models import ActionPlan
from app.tasks.releases.activities import ReleaseActivities
from app.tasks.releases.models import ReleaseAssessment
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import ApprovalResponse, HumanAnswer, WorkflowProgress
from app.triggers.activities import EventActivities
from app.triggers.schemas import EventReceipt, NormalizedEvent
from app.triggers.service import EventService


def demo_settings(queue: str, *, timeout: float = 120, auto_pause: bool = True) -> Settings:
    rules = (
        [
            {
                "id": "fake-release-pause",
                "risk_levels": ["L3"],
                "environments": ["test"],
                "action_names": ["pause_release"],
                "decision": "allow",
                "reason": "仅 Fake 演示允许暂停，回滚仍需单独审批",
            }
        ]
        if auto_pause
        else []
    )
    return Settings(
        APP_ENV="test",
        RELEASE_CONFIG={"enabled": True, "observation_seconds": 0.1},
        EXECUTION_CONFIG={"enabled": True},
        POLICY_CONFIG={"rules": rules},
        TEMPORAL_CONFIG={
            "task_queue": queue,
            "human_timeout_seconds": timeout,
            "activity_timeout_seconds": 30,
            "activity_max_attempts": 2,
        },
    )


async def push_release(
    database: Database, settings: Settings, state: FakeReleaseState
) -> EventReceipt:
    release_id = "release-" + uuid4().hex
    state.add(release_id)
    await DiscoveryActivities(database, settings).refresh(
        DiscoveryRequest(utc_now().isoformat(), 900)
    )
    async with database.session() as session, session.begin():
        return (
            await EventService(session).accept(
                [
                    NormalizedEvent(
                        origin="ops_platform",
                        source=TaskSource.RELEASE,
                        external_id=release_id,
                        service_name="payment-service",
                        title="支付服务 v2.3.6 → v2.3.7 发布申请",
                        occurred_at=utc_now(),
                    )
                ]
            )
        )[0]


async def wait_phase(
    handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress], purpose: str
) -> WorkflowProgress:
    async with asyncio.timeout(60):
        while True:
            progress = await handle.query(AITaskWorkflow.progress)
            if (
                progress.task
                and progress.task.status is TaskStatus.WAITING_APPROVAL
                and progress.approval_prompt
                and progress.action_plan_json
            ):
                plan = ActionPlan.model_validate_json(progress.action_plan_json)
                if (
                    plan.actions[0].action.id == "release-" + purpose
                    and progress.approval_prompt.plan_evidence_id
                    == progress.action_plan_evidence_id
                ):
                    return progress
            if progress.task and progress.task.status in {
                TaskStatus.ESCALATED,
                TaskStatus.AUTOMATION_ABORTED,
                TaskStatus.CLOSED,
            }:
                raise AssertionError(f"发布在 {purpose} 审批前停止：{progress}")
            await asyncio.sleep(0.05)


async def approve_phase(
    handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress],
    purpose: str,
    *,
    decision: str = "approved",
    interactive: bool = False,
) -> bool:
    progress = await wait_phase(handle, purpose)
    prompt = progress.approval_prompt
    assert prompt is not None
    if interactive:
        print("待审批阶段：" + purpose, flush=True)
        print("动作计划：" + str(progress.action_plan_json), flush=True)
        print("请输入“批准”或“拒绝”：", flush=True)
        decision = "approved" if await asyncio.to_thread(input) == "批准" else "rejected"
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
    return decision == "approved"


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("发布演示只允许本机隔离临时库与 Temporal")
    database = Database(url)
    client = await Client.connect(
        address, namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default")
    )
    handles: list[WorkflowHandle[AITaskWorkflow, WorkflowProgress]] = []
    try:
        for scenario in ("normal", "anomaly", "high_sql"):
            settings = demo_settings("release-demo-" + uuid4().hex)
            state = FakeReleaseState(scenario=scenario)
            activities = ReleaseActivities(database, settings, state=state)
            receipt = await push_release(database, settings, state)
            print("发布场景：" + scenario, flush=True)
            async with create_worker(client, database, settings, release_activities=activities):
                await EventActivities(database, settings, client).start_task(receipt)
                handle = client.get_workflow_handle(
                    receipt.workflow_id, result_type=WorkflowProgress
                )
                handles.append(handle)
                if scenario == "high_sql":
                    from app.tasks.tickets.demo import wait_progress

                    progress = await wait_progress(handle, TaskStatus.NEED_HUMAN_JUDGMENT)
                    assert progress.conclusion_json and progress.human_prompt
                    assessment = ReleaseAssessment.model_validate_json(progress.conclusion_json)
                    assert not next(c for c in assessment.checks if c.name == "sql").passed
                    print(
                        "高风险 SQL 已标记（至少 L4）；NEED_HUMAN_JUDGMENT；签发/执行均为 0",
                        flush=True,
                    )
                    prompt = progress.human_prompt
                    await handle.signal(
                        AITaskWorkflow.answer_question,
                        HumanAnswer(
                            prompt.question_id,
                            prompt.task.status,
                            prompt.task.version,
                            "先评审 SQL，再修订源发布申请。",
                            "demo-operator",
                        ),
                    )
                else:
                    approved = await approve_phase(handle, "canary", interactive=interactive)
                    next_purpose = "promote" if scenario == "normal" else "rollback"
                    stage_progress = await wait_phase(handle, next_purpose) if approved else None
                    if approved and scenario == "anomaly":
                        target = state.writer.targets["payment-service"]
                        assert (
                            target.paused
                            and target.image.endswith(":v2.3.7")
                            and state.writer.execution_count == 2
                        )
                        assert (
                            stage_progress
                            and stage_progress.action_plan_json
                            and "need_approval" in stage_progress.action_plan_json
                        )
                        print(
                            "5xx 异常 → 停止推广 → 经 Policy 暂停；回滚为独立 L3 审批，旧批准无效",
                            flush=True,
                        )
                    if approved:
                        await approve_phase(handle, next_purpose, interactive=interactive)
                result = await asyncio.wait_for(handle.result(), 60)
                assert result.task is not None
                if result.task.status is TaskStatus.CLOSED:
                    assert result.postmortem_evidence_id
                    async with database.session() as session:
                        report = await LedgerService(session).get_evidence(
                            UUID(result.postmortem_evidence_id)
                        )
                        print(
                            "发布报告：" + json.dumps(report.result_snapshot, ensure_ascii=False),
                            flush=True,
                        )
                else:
                    assert result.task.status is TaskStatus.ESCALATED
                print(
                    f"任务状态：{result.task.status.value}；"
                    f"Fake 动作次数：{state.writer.execution_count}；"
                    f"Workflow：{receipt.workflow_id}",
                    flush=True,
                )
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
        print("Step 39 发布与变更场景 Fake 演示全部通过", flush=True)
    finally:
        for handle in handles:
            if (await handle.describe()).close_time is None:
                await handle.terminate("发布演示清理")
        await database.dispose()
