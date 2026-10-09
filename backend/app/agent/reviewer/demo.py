"""Step 27 人工验收：本机隔离 Workflow、Ledger 与两套明确的 Fake 事实。"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities
from app.agent.investigation import InvestigationSpec
from app.agent.reviewer.activities import ReviewerActivities
from app.agent.reviewer.models import ReviewDecision
from app.config import Settings
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.planning.activities import PlanningActivities
from app.tasks.review_gate import ReviewRequired
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest, WorkflowInput
from app.tools.reviewer_fake import fake_review_registry


async def run_demo(url: URL) -> None:
    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"reviewer-demo-{uuid4().hex}",
        },
    )
    client = await Client.connect(settings.temporal_config.address)
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    spec = InvestigationSpec(
        service_name="payment-service",
        title="支付 5xx Reviewer 反证验收",
        start=end - timedelta(hours=1),
        end=end,
    )
    handles = []
    try:
        await DiscoveryActivities(database, settings).refresh(
            DiscoveryRequest(end.isoformat(), 3600)
        )
        await TimelineActivities(database, settings).collect(
            TimelineRequest(spec.service_name, spec.start.isoformat(), spec.end.isoformat())
        )
        async with database.session() as session, session.begin():
            task = await TaskService(session).create(
                source=TaskSource.ALERT, title=spec.title, reason="Step 27 门禁验收"
            )
        store = TaskActivityStore(database)
        snapshot = TaskSnapshot(str(task.id), task.status, task.status_version)
        for status in (
            TaskStatus.CONTEXT_BUILDING,
            TaskStatus.RUNBOOK_MATCHING,
            TaskStatus.INVESTIGATING,
            TaskStatus.RCA,
        ):
            snapshot = await store.transition(TransitionRequest(snapshot, status, "准备门禁验收"))
        try:
            await store.transition(
                TransitionRequest(snapshot, TaskStatus.PLANNING, "跳过 Reviewer")
            )
        except ReviewRequired:
            print("关键任务跳过 Reviewer → PLANNING 被拒绝：通过。", flush=True)
        else:
            raise AssertionError("关键任务绕过了 Reviewer")
        modes: tuple[Literal["clear", "contradicted"], ...] = ("clear", "contradicted")
        for mode in modes:
            async with database.session() as session, session.begin():
                task = await TaskService(session).create(
                    source=TaskSource.ALERT, title=spec.title, reason=f"Step 27 Fake {mode}"
                )
            tasks, agent = TaskActivities(store), AgentActivities(database, settings)
            reviewer = ReviewerActivities(
                database, settings, registry_factory=partial(fake_review_registry, mode=mode)
            )
            async with Worker(
                client,
                task_queue=settings.temporal_config.task_queue,
                workflows=[AITaskWorkflow],
                activities=[
                    tasks.load,
                    tasks.transition,
                    tasks.placeholder_stage,
                    agent.investigate,
                    agent.validate_conclusion,
                    RunbookActivities(database, settings).match,
                    reviewer.review,
                    PlanningActivities(database, settings).plan,
                ],
            ):
                handle = await start_task_workflow(
                    client,
                    WorkflowInput(str(task.id), investigation_json=spec.model_dump_json()),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(handle)
                if mode == "clear":
                    async with asyncio.timeout(30):
                        while True:
                            progress = await handle.query(AITaskWorkflow.progress)
                            if (
                                progress.task
                                and progress.task.status is TaskStatus.WAITING_APPROVAL
                            ):
                                break
                            if progress.task and progress.task.status is TaskStatus.ESCALATED:
                                raise AssertionError("无反证场景意外转人工")
                            await asyncio.sleep(0.05)
                else:
                    progress = await asyncio.wait_for(handle.result(), 30)
                assert progress.review_json and progress.review_evidence_id
                decision = ReviewDecision.model_validate_json(progress.review_json)
                states = [item.status for item in progress.history]
                assert TaskStatus.EXECUTING not in states
                if mode == "clear":
                    assert decision.conclusion.confidence == 0.8
                    print("无反证：置信度 0.7 → 0.8；四类替代原因复核通过。", flush=True)
                    print("经 Step 28 生成计划，状态 WAITING_APPROVAL。", flush=True)
                else:
                    assert (
                        decision.conclusion.confidence == 0.5
                        and states[-1] is TaskStatus.AUTOMATION_ABORTED
                    )
                    print("网络超时反证：置信度 0.7 → 0.5；证据冲突触发熔断。", flush=True)
                    print("RCA → AUTOMATION_ABORTED，接管通知已发送。", flush=True)
                print(f"Workflow ID：{handle.id}", flush=True)
                print(f"复核 Evidence ID：{progress.review_evidence_id}", flush=True)
                async with database.session() as session:
                    ledger = LedgerService(session)
                    review = await ledger.get_evidence(UUID(progress.review_evidence_id))
                    assert review.result_snapshot == json.loads(progress.review_json)
                    for reference in decision.report.evidence_ids:
                        assert (await ledger.get_evidence(reference)).task_id == task.id
                    for check in decision.report.checks:
                        print(
                            f"  {check.alternative.value}：{check.outcome}；"
                            f"证据 {check.evidence_ids}",
                            flush=True,
                        )
                    history = await TaskService(session).history(task.id)
                    assert [(item.to_status, item.sequence) for item in history] == [
                        (item.status, item.version) for item in progress.history
                    ]
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
        print("Step 27 Reviewer Fake 演示全部通过。", flush=True)
    finally:
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 27 演示结束，清理隔离任务")
        await database.dispose()
