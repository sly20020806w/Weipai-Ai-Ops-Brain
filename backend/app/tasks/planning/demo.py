"""Step 28 人工验收：隔离本机数据库、Worker 和明确的 Fake 回滚计划。"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer

from app.agent.investigation import InvestigationSpec
from app.config import Settings
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.service import LedgerService
from app.tasks.planning.models import ActionPlan, ActionPlanDraft
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import ApprovalResponse, HumanResponse, WorkflowInput


async def run_demo(url: URL) -> None:
    if url.host != "127.0.0.1" or not (url.database or "").startswith("weipai_db_test_"):
        raise ValueError("Action Plan 演示只允许本机独立临时库")
    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"action-plan-demo-{uuid4().hex}",
        },
    )
    client = await Client.connect(settings.temporal_config.address)
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    spec = InvestigationSpec(
        service_name="payment-service",
        title="支付 5xx 回滚方案验收",
        start=end - timedelta(hours=1),
        end=end,
    )
    handle = None
    try:
        await DiscoveryActivities(database, settings).refresh(
            DiscoveryRequest(end.isoformat(), 3600)
        )
        await TimelineActivities(database, settings).collect(
            TimelineRequest(spec.service_name, spec.start.isoformat(), spec.end.isoformat())
        )
        async with database.session() as session, session.begin():
            task = await TaskService(session).create(
                source=TaskSource.ALERT, title=spec.title, reason="Step 28 Fake 演示"
            )
        async with create_worker(client, database, settings, max_cached_workflows=0):
            handle = await start_task_workflow(
                client,
                WorkflowInput(str(task.id), investigation_json=spec.model_dump_json()),
                task_queue=settings.temporal_config.task_queue,
            )
            async with asyncio.timeout(30):
                while True:
                    progress = await handle.query(AITaskWorkflow.progress)
                    if (
                        progress.task
                        and progress.task.status is TaskStatus.WAITING_APPROVAL
                        and progress.approval_prompt
                    ):
                        break
                    if progress.task and progress.task.status is TaskStatus.ESCALATED:
                        raise AssertionError("计划生成意外失败")
                    await asyncio.sleep(0.05)
            assert progress.action_plan_json and progress.action_plan_evidence_id
            plan = ActionPlan.model_validate_json(progress.action_plan_json)
            action = plan.actions[0]
            assert action.action.parameters == {"from_version": "v2.3.7", "to_version": "v2.3.6"}
            assert (
                action.policy.risk_level.value == "L3"
                and action.policy.decision.value == "need_approval"
            )
            print("回滚 payment-service v2.3.7 → v2.3.6：L3 / need_approval。", flush=True)
            print("状态：RCA → PLANNING → WAITING_APPROVAL。", flush=True)
            print(f"Workflow ID：{handle.id}", flush=True)
            print(f"Action Plan Evidence ID：{progress.action_plan_evidence_id}", flush=True)
            print(f"依据 Evidence ID：{plan.summary.evidence_ids}", flush=True)
            print(f"回滚方案：{action.action.rollback.description}", flush=True)
            print(f"验证方式：{action.action.verification.checks}", flush=True)
            for field in ("rollback", "verification"):
                data = json.loads(
                    ActionPlanDraft(
                        summary=plan.summary, actions=(action.action,)
                    ).model_dump_json()
                )
                del data["actions"][0][field]
                try:
                    ActionPlanDraft.model_validate_json(json.dumps(data))
                except ValidationError:
                    print(f"缺少 {field} 的动作无法进入计划：通过。", flush=True)
                else:
                    raise AssertionError("计划必填检查失效")
            async with database.session() as session:
                ledger = LedgerService(session)
                assert (
                    await ledger.get_evidence(UUID(progress.action_plan_evidence_id))
                ).result_snapshot == json.loads(progress.action_plan_json)
                history = await TaskService(session).history(task.id)
                assert [(item.to_status, item.sequence) for item in history] == [
                    (item.status, item.version) for item in progress.history
                ]
                assert all(
                    item.operation != "execute_action"
                    for item in await ledger.audits_for_task(task.id)
                )
            assert progress.task
            await handle.signal(
                AITaskWorkflow.human_response,
                HumanResponse(progress.task.status, progress.task.version, True),
            )
            assert (await handle.query(AITaskWorkflow.progress)).task == progress.task
            prompt = progress.approval_prompt
            assert prompt
            await handle.signal(
                AITaskWorkflow.approve_actions,
                ApprovalResponse(
                    prompt.task.task_id,
                    prompt.approval_id,
                    prompt.task.version,
                    prompt.action_hash,
                    "rejected",
                    "local-owner",
                ),
            )
            finished = await asyncio.wait_for(handle.result(), 30)
            assert finished.task and finished.task.status is TaskStatus.ESCALATED
            assert TaskStatus.EXECUTING not in [item.status for item in finished.history]
            print("普通人工信号不能审批；明确拒绝后转交人工且不执行：通过。", flush=True)
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
        print("Step 28 Action Plan Fake 演示全部通过。", flush=True)
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 28 演示结束，清理隔离任务")
        await database.dispose()
