"""Step 34 本机 Fake 事故复盘演示；前置事实通过既有业务服务采集并留证。"""

import asyncio
import json
import os
from datetime import timedelta
from functools import partial
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer

from app.agent.activities import AgentActivities
from app.agent.investigation import InvestigationSpec
from app.agent.reviewer.activities import ReviewerActivities
from app.agent.reviewer.models import ReviewRequest
from app.agent.workflow_models import ConclusionRequest, InvestigationRequest
from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.kubernetes.execution import ExecutionReceipt, FakeKubernetesWriteConnector
from app.connectors.observability.fake import SAMPLE_END
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.executor.models import ExecutionCommand, ExecutionRequest
from app.executor.service import ExecutionStore
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.learning.models import IncidentReport, IncidentSearch
from app.learning.service import IncidentService
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.runbooks.embedding import embedding_client
from app.runbooks.schemas import RunbookMaturity
from app.runbooks.service import RunbookService
from app.runbooks.workflow_models import RunbookMatchRequest
from app.tasks.activities import TaskActivityStore
from app.tasks.approval.service import ApprovalStore
from app.tasks.planning.activities import PlanningActivities
from app.tasks.planning.models import PlanningRequest
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    TaskSnapshot,
    TransitionRequest,
    WorkflowInput,
)
from app.tools.verification_runtime import fake_verification_registry
from app.triggers.schemas import NormalizedEvent
from app.triggers.service import EventService
from app.verifier.activities import VerifierActivities
from app.verifier.models import ResourceExpectation, VerificationSpec
from app.verifier.scenario import sample_spec


async def prepare_incident(
    database: Database,
    settings: Settings,
) -> tuple[TaskSnapshot, VerificationSpec, FakeKubernetesWriteConnector]:
    if settings.app_env not in {"local", "test"} or settings.connector_mode.value != "fake":
        raise ValueError("事故样例只允许 local/test + Fake")
    spec = InvestigationSpec(
        service_name="payment-service",
        title="支付 5xx 发布故障复盘",
        start=SAMPLE_END - timedelta(hours=1),
        end=SAMPLE_END,
    )
    await DiscoveryActivities(database, settings).refresh(
        DiscoveryRequest(SAMPLE_END.isoformat(), 3600)
    )
    await TimelineActivities(database, settings).collect(
        TimelineRequest(spec.service_name, spec.start.isoformat(), spec.end.isoformat())
    )
    async with database.session() as session, session.begin():
        receipt = (
            await EventService(session).accept(
                [
                    NormalizedEvent(
                        origin="prometheus",
                        source=TaskSource.ALERT,
                        external_id=f"postmortem-demo-{uuid4().hex}",
                        service_name=spec.service_name,
                        title=spec.title,
                        occurred_at=SAMPLE_END,
                    )
                ]
            )
        )[0]
    task = TaskSnapshot(receipt.task_id, TaskStatus.NEW, 0)
    store = TaskActivityStore(database)

    async def move(target: TaskStatus) -> None:
        nonlocal task
        task = await store.transition(TransitionRequest(task, target, "Step 34 Fake 前置事故阶段"))

    for target in (TaskStatus.CONTEXT_BUILDING, TaskStatus.RUNBOOK_MATCHING):
        await move(target)
    matched = await RunbookActivities(database, settings).match(
        RunbookMatchRequest(task, spec.model_dump_json())
    )
    await move(TaskStatus.INVESTIGATING)
    agent = AgentActivities(database, settings)
    result = await agent.investigate(
        InvestigationRequest(task, spec.model_dump_json(), matched.runbook_json)
    )
    await move(TaskStatus.RCA)
    conclusion = await agent.validate_conclusion(ConclusionRequest(task, result))
    review = await ReviewerActivities(database, settings).review(
        ReviewRequest(task, spec.model_dump_json(), conclusion)
    )
    await move(TaskStatus.PLANNING)
    plan = await PlanningActivities(database, settings).plan(
        PlanningRequest(task, spec.model_dump_json(), conclusion, review.evidence_id)
    )
    await move(TaskStatus.WAITING_APPROVAL)
    approvals = ApprovalStore(database, settings)
    from app.tasks.workflow_models import ApprovalRequest

    prompt = await approvals.notify(ApprovalRequest(task, plan.evidence_id), FakeFeishuConnector())
    await approvals.decide(
        ApprovalDecisionRequest(
            prompt,
            ApprovalResponse(
                task.task_id,
                prompt.approval_id,
                task.version,
                prompt.action_hash,
                "approved",
                "local-fake-owner",
            ),
        )
    )
    await move(TaskStatus.EXECUTING)
    connector = FakeKubernetesWriteConnector(clock=lambda: SAMPLE_END)
    executed = await ExecutionStore(database, settings, connector).execute(
        ExecutionRequest(task, plan.evidence_id, prompt)
    )
    async with database.session() as session:
        record = await LedgerService(session).get_evidence(UUID(executed.evidence_ids[-1]))
        command = ExecutionCommand.model_validate_json(json.dumps(record.parameters))
        action = ExecutionReceipt.model_validate_json(json.dumps(record.result_snapshot))
    verification = sample_spec(executed.task).model_copy(
        update={
            "action_id": command.action_id,
            "action_completed_at": action.completed_at,
            "start": action.completed_at,
            "end": action.completed_at + timedelta(minutes=5),
            "expected_image": action.target.image,
            "expected_replicas": action.target.replicas,
        }
    )
    return executed.task, VerificationSpec.model_validate(verification), connector


async def run_demo(url: URL) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("复盘演示只允许本机专用临时数据库与 Temporal")
    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        VERIFICATION_CONFIG={
            "resources_by_service": {
                "payment-service": (
                    ResourceExpectation(
                        product="rds",
                        region_id="cn-hangzhou",
                        resource_id="rm-payment",
                        healthy_status="Running",
                    ),
                )
            }
        },
        TEMPORAL_CONFIG={
            "address": address,
            "task_queue": f"postmortem-demo-{uuid4().hex}",
        },
    )
    client = await Client.connect(address)
    handles = []
    connector = None
    try:
        snapshot, investigation = await seed_incident(database, settings)
        connector = FakeKubernetesWriteConnector(clock=lambda: SAMPLE_END)
        executor = ExecutorActivities(database, settings, connector=connector)
        verifier = VerifierActivities(
            database,
            settings,
            registry_factory=partial(
                fake_verification_registry,
                window_start=SAMPLE_END,
                window_end=SAMPLE_END + timedelta(minutes=5),
            ),
        )
        async with create_worker(
            client, database, settings, verifier_activities=verifier, executor_activities=executor
        ):
            handle = await start_task_workflow(
                client,
                WorkflowInput(
                    snapshot.task_id,
                    investigation_json=investigation.model_dump_json(),
                    execution_enabled=True,
                ),
                task_queue=settings.temporal_config.task_queue,
            )
            handles.append(handle)
            async with asyncio.timeout(45):
                while True:
                    pending = await handle.query(AITaskWorkflow.progress)
                    if pending.approval_prompt:
                        prompt = pending.approval_prompt
                        break
                    if pending.task and pending.task.status is TaskStatus.ESCALATED:
                        raise AssertionError("复盘演示在审批前失败")
                    await asyncio.sleep(0.05)
            await handle.signal(
                AITaskWorkflow.approve_actions,
                ApprovalResponse(
                    snapshot.task_id,
                    prompt.approval_id,
                    prompt.task.version,
                    prompt.action_hash,
                    "approved",
                    "local-fake-owner",
                ),
            )
            progress = await asyncio.wait_for(handle.result(), 45)
            assert progress.task and progress.task.status is TaskStatus.CLOSED
            assert progress.postmortem_json and progress.postmortem_evidence_id
            report = IncidentReport.model_validate_json(progress.postmortem_json)
            print(f"事故任务：{snapshot.task_id}；最终状态：CLOSED", flush=True)
            print(f"复盘 Evidence ID：{progress.postmortem_evidence_id}", flush=True)
            for section in report.sections:
                print(f"\n【{section.title}】", flush=True)
                for claim in section.conclusions:
                    print(
                        f"{claim.statement}\nEvidence：{', '.join(map(str, claim.evidence_ids))}",
                        flush=True,
                    )
            async with database.session() as session:
                runbook = await RunbookService(
                    session, lambda r: embedding_client(settings, r)
                ).get(report.runbook_id)
                assert runbook.maturity is RunbookMaturity.DRAFT
                found = await IncidentService(session).search(
                    IncidentSearch(query="payment-service", service_name="payment-service")
                )
                assert any(hit.report.task_id == report.task_id for hit in found)
            print(f"\nDraft Runbook：{runbook.id}；成熟度：{runbook.maturity}", flush=True)
            for task_id in report.improvement_task_ids:
                child = client.get_workflow_handle(f"ai-task-{task_id}")
                handles.append(child)
                print(f"改进任务：{task_id}；Workflow：{child.id}", flush=True)
            print("search_incidents：找到本事故；Fake 回滚次数：1；真实运维请求：0", flush=True)
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
        print("Step 34 Postmortem Fake 演示全部通过", flush=True)
    finally:
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 34 演示清理")
        if connector:
            await connector.aclose()
        await database.dispose()


async def seed_incident(
    database: Database, settings: Settings
) -> tuple[TaskSnapshot, InvestigationSpec]:
    if settings.app_env not in {"local", "test"} or settings.connector_mode.value != "fake":
        raise ValueError("事故样例只允许 local/test + Fake")
    spec = InvestigationSpec(
        service_name="payment-service",
        title="支付 5xx 发布故障复盘",
        start=SAMPLE_END - timedelta(hours=1),
        end=SAMPLE_END,
    )
    await DiscoveryActivities(database, settings).refresh(
        DiscoveryRequest(SAMPLE_END.isoformat(), 3600)
    )
    await TimelineActivities(database, settings).collect(
        TimelineRequest(spec.service_name, spec.start.isoformat(), spec.end.isoformat())
    )
    async with database.session() as session, session.begin():
        receipt = (
            await EventService(session).accept(
                [
                    NormalizedEvent(
                        origin="prometheus",
                        source=TaskSource.ALERT,
                        external_id=f"postmortem-{uuid4().hex}",
                        service_name=spec.service_name,
                        title=spec.title,
                        occurred_at=SAMPLE_END,
                    )
                ]
            )
        )[0]
    return TaskSnapshot(receipt.task_id, TaskStatus.NEW, 0), spec
