"""本机独立临时库：准备已关闭 Fake 事故，再由 Temporal 回放并显示十项指标。"""

import asyncio
import json
import os
from datetime import timedelta
from functools import partial
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from app.agent.investigation import AgentConclusion, InvestigationSpec
from app.config import Settings
from app.connectors.observability.fake import SAMPLE_END
from app.db.base import utc_now
from app.db.session import Database
from app.learning.demo import prepare_incident
from app.learning.evaluation.activities import ReplayActivities
from app.learning.evaluation.metrics import EvaluationService, EvaluationWindow
from app.learning.evaluation.models import EvaluationLabel, ReplayReport, ReplayRequest
from app.learning.evaluation.workflow import ReplayEvaluationWorkflow
from app.learning.models import LearningRequest
from app.learning.service import LearningStore
from app.ledger.service import LedgerService
from app.tasks.activities import TaskActivityStore
from app.tasks.states import TaskStatus
from app.tasks.workflow_models import TransitionRequest
from app.tools.verification_runtime import fake_verification_registry
from app.verifier.activities import VerifierActivities
from app.verifier.models import VerificationRequest


async def closed_fake_incident(database: Database, settings: Settings) -> ReplayRequest:
    snapshot, verification, connector = await prepare_incident(database, settings)
    try:
        result = await VerifierActivities(
            database,
            settings,
            registry_factory=partial(
                fake_verification_registry,
                window_start=verification.start,
                window_end=verification.end,
            ),
        ).verify(VerificationRequest(snapshot, verification.model_dump_json()))
        assert result.task.status is TaskStatus.RESOLVED
        store = TaskActivityStore(database)
        learning = await store.transition(
            TransitionRequest(result.task, TaskStatus.LEARNING, "Step 36 准备已验证 Fake 历史事故")
        )
        await LearningStore(database, settings).generate(LearningRequest(learning))
        await store.transition(TransitionRequest(learning, TaskStatus.CLOSED, "Fake 复盘完成"))
        async with database.session() as session, session.begin():
            records = await LedgerService(session).evidence_for_task(UUID(snapshot.task_id))
            baseline = next(e for e in records if e.source_tool == "agent.conclusion")
            original = AgentConclusion.model_validate_json(json.dumps(baseline.result_snapshot))
            await EvaluationService(session).label(
                UUID(snapshot.task_id),
                EvaluationLabel(
                    accepted_root_causes=(original.root_cause.statement,),
                    false_alert=False,
                    evidence_ids=(baseline.id,),
                ),
                actor="local-fake-benchmark-owner",
            )
            return ReplayRequest(
                run_id=uuid4(),
                task_id=UUID(snapshot.task_id),
                baseline_evidence_id=baseline.id,
                cutoff=baseline.created_at,
                candidate_version="fake-payment-v2",
                investigation=InvestigationSpec(
                    service_name="payment-service",
                    title="支付 5xx 发布故障复盘",
                    start=SAMPLE_END - timedelta(hours=1),
                    end=SAMPLE_END,
                ),
            )
    finally:
        await connector.aclose()


async def run_demo(url: URL) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("Replay 演示只允许本机隔离临时库与 Temporal")
    database = Database(url)
    settings = Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})
    began = utc_now()
    handle = None
    try:
        request = await closed_fake_incident(database, settings)
        client = await Client.connect(address)
        queue = f"replay-demo-{uuid4().hex}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[ReplayEvaluationWorkflow],
            activities=[ReplayActivities(database, settings).replay],
        ):
            handle = await client.start_workflow(
                ReplayEvaluationWorkflow.run,
                request.model_dump_json(),
                id=f"replay-{request.run_id}",
                task_queue=queue,
            )
            report = ReplayReport.model_validate_json(await asyncio.wait_for(handle.result(), 45))
            assert report.status == "completed" and report.candidate_hit is True
            assert report.candidate_tool_calls == 4 and len(report.observed_evidence_ids) == 4
            await Replayer(workflows=[ReplayEvaluationWorkflow]).replay_workflow(
                await handle.fetch_history()
            )
        print(f"已关闭 Fake 事故：{request.task_id}；Replay Workflow：{handle.id}", flush=True)
        print(
            f"截止时间：{report.cutoff.isoformat()}；候选版本：{report.candidate_version}",
            flush=True,
        )
        print(
            f"根因命中：基线={report.baseline_hit}，候选={report.candidate_hit}；误判={report.misjudged}",
            flush=True,
        )
        print(
            f"Tool Call：基线={report.baseline_tool_calls}，候选={report.candidate_tool_calls}",
            flush=True,
        )
        print(
            f"耗时：基线={report.baseline_elapsed_seconds:.3f}s，候选={report.candidate_elapsed_seconds:.3f}s",
            flush=True,
        )
        print("历史 Evidence：" + ", ".join(map(str, report.observed_evidence_ids)), flush=True)
        print("Replay 注册表没有 Connector 实例和写 Tool；回放期间 Connector 调用：0", flush=True)
        async with database.session() as session:
            metrics = await EvaluationService(session).report(
                EvaluationWindow(start=began, end=utc_now())
            )
        for metric in metrics.metrics:
            value = (
                "未知（暂无对应样本）"
                if metric.value is None
                else f"{metric.value:.4f} {metric.unit}"
            )
            print(
                f"{metric.label}：{value}；分子={metric.numerator:g}，分母={metric.denominator}",
                flush=True,
            )
        print("Step 36 Replay 与 AI 评价 Fake 演示全部通过", flush=True)
    finally:
        if handle:
            from temporalio.client import WorkflowExecutionStatus

            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 36 独立演示清理")
        await database.dispose()
