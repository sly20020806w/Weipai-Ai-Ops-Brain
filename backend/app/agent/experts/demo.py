"""Step 26：本机隔离 Temporal、真实 Ledger 和全 Fake 调查演示。"""

import asyncio
import json
import os
from uuid import uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities, configured_llm
from app.agent.client import LLMClient
from app.agent.experts.demo_scenario import consultation_response
from app.agent.experts.models import ExpertAdvice
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import AgentConclusion, InvestigationSpec
from app.agent.models import ChatRequest
from app.agent.reviewer.activities import ReviewerActivities
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
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import WorkflowInput


async def run_demo(url: URL) -> None:
    # Fake 只读数据的历史窗；不加载任何公司系统凭证。
    from datetime import UTC, datetime, timedelta

    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"expert-demo-{uuid4().hex}",
        },
    )
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    spec = InvestigationSpec(
        service_name="payment-service",
        title="支付 5xx 与容量线索",
        start=end - timedelta(hours=1),
        end=end,
    )
    client = await Client.connect(settings.temporal_config.address)
    handles = []
    try:
        await DiscoveryActivities(database, settings).refresh(
            DiscoveryRequest(end.isoformat(), 3600)
        )
        await TimelineActivities(database, settings).collect(
            TimelineRequest(spec.service_name, spec.start.isoformat(), spec.end.isoformat())
        )
        for mode in ("complex", "simple"):
            async with database.session() as session, session.begin():
                task = await TaskService(session).create(
                    source=TaskSource.ALERT, title=spec.title, reason=f"Step 26 Fake {mode}"
                )

            def factory(request: ChatRequest, scenario: str = mode) -> LLMClient:
                if scenario == "complex":
                    return FakeLLM([ScriptedChatStep(consultation_response)])
                return configured_llm(settings, request)

            agent = AgentActivities(database, settings, llm_factory=factory)
            tasks = TaskActivities(TaskActivityStore(database))
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
                    ReviewerActivities(database, settings).review,
                    PlanningActivities(database, settings).plan,
                    RunbookActivities(database, settings).match,
                ],
            ):
                handle = await start_task_workflow(
                    client,
                    WorkflowInput(str(task.id), investigation_json=spec.model_dump_json()),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(handle)
                async with asyncio.timeout(30):
                    while True:
                        progress = await handle.query(AITaskWorkflow.progress)
                        if progress.task and progress.task.status is TaskStatus.WAITING_APPROVAL:
                            break
                        await asyncio.sleep(0.05)
                assert progress.conclusion_json and progress.conclusion_evidence_id
                async with database.session() as session:
                    ledger = LedgerService(session)
                    evidence = await ledger.evidence_for_task(task.id)
                    opinions = [item for item in evidence if item.source_tool == "consult_expert"]
                    if mode == "complex":
                        assert len(opinions) == 2
                        for item in opinions:
                            advice = ExpertAdvice.model_validate_json(
                                json.dumps(item.result_snapshot)
                            )
                            for reference in advice.opinion.evidence_ids:
                                assert (await ledger.get_evidence(reference)).task_id == task.id
                            print(f"{advice.expert.value} 意见 Evidence ID：{item.id}", flush=True)
                            print(f"  {advice.opinion.assessment.statement}", flush=True)
                        print("专家意见的事实引用已逐条读库验证。", flush=True)
                    else:
                        assert not opinions
                        print("简单场景：专家调用 0 次。", flush=True)
                conclusion = AgentConclusion.model_validate_json(progress.conclusion_json)
                print(f"主 Agent 结论：{conclusion.root_cause.statement}", flush=True)
                print(f"Workflow ID：{handle.id}；状态 WAITING_APPROVAL。", flush=True)
                assert TaskStatus.EXECUTING not in [item.status for item in progress.history]
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
        print("Step 26 专家 Fake 演示全部通过。", flush=True)
    finally:
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 26 演示结束，清理隔离任务")
        await database.dispose()
