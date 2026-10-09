"""独立临时库/Temporal 队列的 Step 24 Fake 人工验收。"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities, configured_llm
from app.agent.client import LLMClient
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import AgentConclusion, InvestigationSpec
from app.agent.models import ChatRequest
from app.agent.reviewer.activities import ReviewerActivities
from app.agent.scenario import PAYMENT_TOOLS, invalid_conclusion_response
from app.config import Settings
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.planning.activities import PlanningActivities
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import WorkflowInput


class _ExpectedDemoRejection(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # 演示断言并展示这两种预期拒绝；其他错误保留完整日志。
        error = record.exc_info[1] if record.exc_info else None
        return not (
            isinstance(error, ApplicationError)
            and error.type in {"InvalidConclusion", "AgentStepLimit"}
        )


async def run_demo(url: URL) -> None:
    log_filter = _ExpectedDemoRejection()
    activity_logger = logging.getLogger("temporalio.activity")
    activity_logger.addFilter(log_filter)
    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"agent-demo-{uuid4().hex}",
        },
    )
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    spec = InvestigationSpec(
        service_name="payment-service",
        title="payment-service 5xx 告警",
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
        for mode in ("valid", "invalid", "limit"):
            async with database.session() as session, session.begin():
                task = await TaskService(session).create(
                    source=TaskSource.ALERT, title=spec.title, reason=f"Step 24 Fake 演示：{mode}"
                )

            def factory(request: ChatRequest, scenario: str = mode) -> LLMClient:
                if scenario == "invalid" and sum(m.role == "tool" for m in request.messages) == 4:
                    return FakeLLM([ScriptedChatStep(invalid_conclusion_response)])
                return configured_llm(settings, request)

            agent = AgentActivities(database, settings, llm_factory=factory)
            activities = TaskActivities(TaskActivityStore(database))
            worker = Worker(
                client,
                task_queue=settings.temporal_config.task_queue,
                workflows=[AITaskWorkflow],
                activities=[
                    activities.load,
                    activities.transition,
                    activities.placeholder_stage,
                    agent.investigate,
                    agent.validate_conclusion,
                    ReviewerActivities(database, settings).review,
                    PlanningActivities(database, settings).plan,
                    RunbookActivities(database, settings).match,
                ],
            )
            current = spec.model_copy(update={"max_steps": 3}) if mode == "limit" else spec
            async with worker:
                handle = await start_task_workflow(
                    client,
                    WorkflowInput(str(task.id), investigation_json=current.model_dump_json()),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(handle)
                if mode == "valid":
                    async with asyncio.timeout(30):
                        while True:
                            progress = await handle.query(AITaskWorkflow.progress)
                            if (
                                progress.task
                                and progress.task.status is TaskStatus.WAITING_APPROVAL
                            ):
                                break
                            await asyncio.sleep(0.05)
                    assert progress.conclusion_json and progress.conclusion_evidence_id
                    conclusion = AgentConclusion.model_validate_json(progress.conclusion_json)
                    print(f"正常场景 Task ID：{task.id}；Workflow ID：{handle.id}", flush=True)
                    print(
                        "Think→Plan→Tool→Observe→Reason：4 次高级查询、5 次 LLM 轮次。", flush=True
                    )
                    async with database.session() as session:
                        ledger = LedgerService(session)
                        evidence = await ledger.evidence_for_task(task.id)
                        tool_evidence = [
                            item for item in evidence if item.id in conclusion.evidence_ids
                        ]
                        assert conclusion.evidence_ids == {item.id for item in tool_evidence}
                        assert [item.source_tool for item in tool_evidence] == list(PAYMENT_TOOLS)
                        audits = [
                            item
                            for item in await ledger.audits_for_task(task.id)
                            if item.event_type is AuditEventType.TOOL_CALL
                        ]
                        assert len(audits) == 7
                        for item in tool_evidence:
                            assert await ledger.get_evidence(item.id) is item
                            print(f"  {item.source_tool} → Evidence ID：{item.id}", flush=True)
                        accepted = await ledger.get_evidence(UUID(progress.conclusion_evidence_id))
                        assert accepted.result_snapshot == json.loads(progress.conclusion_json)
                    print(f"根因线索：{conclusion.root_cause.statement}", flush=True)
                    print(f"结构化结论 Evidence ID：{progress.conclusion_evidence_id}", flush=True)
                    print(
                        "结论引用已逐条读库验证；经 PLANNING 暂停到 WAITING_APPROVAL。",
                        flush=True,
                    )
                else:
                    progress = await asyncio.wait_for(handle.result(), 30)
                    assert progress.task and progress.task.status is TaskStatus.ESCALATED
                    assert progress.conclusion_evidence_id is None
                    print(
                        "不存在的 Evidence ID 被拒绝：ESCALATED"
                        if mode == "invalid"
                        else "最大步数为 3，仅执行 1 次查询后转交人工：ESCALATED",
                        flush=True,
                    )
                assert TaskStatus.EXECUTING not in [item.status for item in progress.history]
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
        print("Step 24 主 Agent 演示全部通过；全部使用 Fake，无生产访问。", flush=True)
    finally:
        activity_logger.removeFilter(log_filter)
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 24 演示结束，清理隔离任务")
        await database.dispose()
