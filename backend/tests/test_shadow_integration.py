"""Step 58 本地影子演练：真实本机数据库/Temporal，全 Fake 来源与 LLM。"""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer, Worker

from app.agent.investigation import AgentConclusion
from app.agent.reviewer.models import ReviewDecision
from app.api.events import get_event_gateway
from app.api.main import create_app
from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector
from app.connectors.observability import fake as observability_fake
from app.db.base import utc_now
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.learning.evaluation.acceptance import AcceptanceCriteria, evaluate_benchmark
from app.learning.evaluation.activities import ReplayActivities
from app.learning.evaluation.demo import closed_fake_incident
from app.learning.evaluation.metrics import EvaluationService, EvaluationWindow
from app.learning.evaluation.models import EvaluationLabel, ReplayReport
from app.learning.evaluation.workflow import ReplayEvaluationWorkflow
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyAction, PolicyDecision, RiskLevel
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.states import TaskStatus
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import WorkflowProgress
from app.triggers.gateway import EventGateway
from tests.e2e_support import NetworkGuard
from tests.test_e2e_integration import ALERT_AT, database, network_guard
from tests.test_evaluation_integration import forbid_live_handlers
from tests.test_events import alert_body, signed

__all__ = ["database", "network_guard"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("TEST_TEMPORAL_ADDRESS"),
        reason="运行 check-shadow.ps1 提供本机隔离数据库/Temporal",
    ),
]
# 独立验收材料，由已知 Fake 场景定义，不能从被评模型的输出生成标签。
EXPECTED_ROOT_CAUSE = "数据库连接池等待超时是当前故障线索，近期发布为待验证的诱因。"


def save_report(name: str, data: dict[str, object]) -> None:
    directory = os.environ.get("STEP58_REPORT_DIRECTORY")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("sample", [1, 2, 3])
async def test_l0_shadow_alert_rca_review_label_and_zero_writes(
    database: Database,
    network_guard: NetworkGuard,
    monkeypatch: pytest.MonkeyPatch,
    sample: int,
) -> None:
    monkeypatch.setattr(observability_fake, "SAMPLE_START", ALERT_AT - timedelta(minutes=10))
    monkeypatch.setattr(observability_fake, "SAMPLE_END", ALERT_AT)
    began = utc_now()
    settings = Settings(
        APP_ENV="test",
        AGENT_CONFIG={"enabled": True},
        EXECUTION_CONFIG={"enabled": False},
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "shadow_no_writes",
                    "risk_levels": ["L1", "L2", "L3", "L4", "L5"],
                    "decision": "deny",
                    "reason": "本地影子演练只允许 L0 调查",
                }
            ]
        },
        TRIGGER_CONFIG={
            "webhook_secrets": {"prometheus": "offline-test-signature-key-32-characters"}
        },
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"shadow-{uuid4().hex}",
        },
    )
    engine = create_policy_engine(settings)
    assert (
        engine.evaluate(PolicyAction(name="query_logs", risk_level=RiskLevel.L0)).decision
        is PolicyDecision.ALLOW
    )
    for risk in tuple(RiskLevel)[1:]:
        assert (
            engine.evaluate(PolicyAction(name="test_action", risk_level=risk)).decision
            is PolicyDecision.DENY
        )
    assert engine.evaluate(PolicyAction(name="undeclared_action")).decision is PolicyDecision.DENY
    await DiscoveryActivities(database, settings).refresh(
        DiscoveryRequest(ALERT_AT.isoformat(), 3600)
    )
    await TimelineActivities(database, settings).collect(
        TimelineRequest(
            "payment-service", (ALERT_AT - timedelta(hours=1)).isoformat(), ALERT_AT.isoformat()
        )
    )
    client = await Client.connect(settings.temporal_config.address)
    connector = FakeKubernetesWriteConnector()
    app = create_app(settings)
    app.state.database = database
    app.dependency_overrides[get_event_gateway] = lambda: EventGateway(client, settings)
    body_data = json.loads(alert_body(starts=ALERT_AT.isoformat()))
    body_data["alerts"][0]["labels"]["shadow_case"] = uuid4().hex
    body = json.dumps(body_data).encode()
    handle = None
    try:
        async with (
            create_worker(
                client,
                database,
                settings,
                feishu_connector=FakeFeishuConnector(),
                executor_activities=ExecutorActivities(database, settings, connector=connector),
            ),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as http,
        ):
            assert (await http.post("/webhooks/prometheus", content=body)).status_code == 401
            response = await http.post("/webhooks/prometheus", content=body, headers=signed(body))
            assert response.status_code == 202, response.text
            receipt = response.json()["events"][0]
            handle = client.get_workflow_handle(
                receipt["workflow_id"], result_type=WorkflowProgress
            )
            progress = await asyncio.wait_for(handle.result(), 60)
            assert progress.task and progress.task.status is TaskStatus.ESCALATED
            assert progress.conclusion_json and progress.review_json and progress.action_plan_json
            conclusion = AgentConclusion.model_validate_json(progress.conclusion_json)
            review = ReviewDecision.model_validate_json(progress.review_json)
            plan = ActionPlan.model_validate_json(progress.action_plan_json)
            assert review.report.verdict == "clear" and plan.decision is PolicyDecision.DENY
            assert progress.approval_prompt is None and not progress.execution_evidence_ids
            assert connector.issue_count == connector.execution_count == 0
            assert all(
                h.status
                not in {
                    TaskStatus.EXECUTING,
                    TaskStatus.VERIFYING,
                    TaskStatus.RESOLVED,
                    TaskStatus.CLOSED,
                }
                for h in progress.history
            )
            task_id = UUID(receipt["task_id"])
            async with database.session() as session, session.begin():
                ledger = LedgerService(session)
                records = await ledger.evidence_for_task(task_id)
                assert conclusion.evidence_ids <= {r.id for r in records}
                label_id = await EvaluationService(session).label(
                    task_id,
                    EvaluationLabel(
                        accepted_root_causes=(EXPECTED_ROOT_CAUSE,),
                        false_alert=False,
                        evidence_ids=(UUID(progress.conclusion_evidence_id or ""),),
                    ),
                    actor="offline-shadow-benchmark-owner",
                )
                audits = await ledger.audits_for_task(task_id)
                assert any(
                    a.operation == "evaluation.label" and a.evidence_id == label_id for a in audits
                )
                assert not any(a.event_type is AuditEventType.EXECUTION for a in audits)
            repeated = await http.post("/webhooks/prometheus", content=body, headers=signed(body))
            assert repeated.json()["events"][0]["task_id"] == str(task_id)
            assert repeated.json()["events"][0]["duplicate"] is True
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
            async with database.session() as session:
                metrics = await EvaluationService(session).report(
                    EvaluationWindow(start=began, end=utc_now())
                )
                own = next(s for s in metrics.samples if s.task_id == task_id)
                assert own.rca_correct is True and not own.execution_attempted
                assert not own.execution_verified and own.mttr_seconds is None
            assert network_guard.rejected == []
            save_report(
                f"shadow-{sample}.json",
                {
                    "scope": "local_fake",
                    "task_id": str(task_id),
                    "workflow_id": handle.id,
                    "rca_evidence_id": progress.conclusion_evidence_id,
                    "review_evidence_id": progress.review_evidence_id,
                    "label_evidence_id": str(label_id),
                    "status": progress.task.status.value,
                    "policy": "deny_L1_L5",
                    "credential_issues": connector.issue_count,
                    "executions": connector.execution_count,
                    "external_network_attempts": len(network_guard.rejected),
                    "metrics": metrics.model_dump(mode="json"),
                },
            )
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 58 本地影子验收清理")
        await connector.aclose()


async def test_replay_benchmark_gate_and_ten_metrics(
    database: Database, network_guard: NetworkGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    began = utc_now()
    settings = Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})
    reports = []
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    handles = []
    try:
        requests = [await closed_fake_incident(database, settings) for _ in range(3)]
        # 覆盖旧演示的自动标签；本次评分用预先定义的独立样例基准。
        for request in requests:
            async with database.session() as session, session.begin():
                await EvaluationService(session).label(
                    request.task_id,
                    EvaluationLabel(
                        accepted_root_causes=(EXPECTED_ROOT_CAUSE,),
                        false_alert=False,
                        evidence_ids=(request.baseline_evidence_id,),
                    ),
                    actor="offline-replay-benchmark-owner",
                )
        invoke = forbid_live_handlers(monkeypatch)
        async with Worker(
            client,
            task_queue=f"shadow-replay-{uuid4().hex}",
            workflows=[ReplayEvaluationWorkflow],
            activities=[ReplayActivities(database, settings).replay],
        ) as worker:
            for request in requests:
                handle = await client.start_workflow(
                    ReplayEvaluationWorkflow.run,
                    request.model_dump_json(),
                    id=f"replay-{request.run_id}",
                    task_queue=worker.task_queue,
                )
                handles.append(handle)
                reports.append(
                    ReplayReport.model_validate_json(await asyncio.wait_for(handle.result(), 60))
                )
                await Replayer(workflows=[ReplayEvaluationWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
        invoke.assert_not_called()
        criteria = AcceptanceCriteria(
            rca_hit_rate=float(os.environ.get("STEP58_RCA_THRESHOLD", "1.0")),
            minimum_samples=int(os.environ.get("STEP58_MINIMUM_SAMPLES", "3")),
        )
        result = evaluate_benchmark(tuple(reports), criteria)
        async with database.session() as session:
            metrics = await EvaluationService(session).report(
                EvaluationWindow(start=began, end=utc_now())
            )
            assert len(metrics.metrics) == 10
            for request in requests:
                task = await session.get(AITask, request.task_id)
                assert task and task.status is TaskStatus.CLOSED
        save_report(
            "benchmark.json",
            {
                "scope": "local_fake",
                "fixture": "three_distinct_incidents_same_payment_scenario",
                "gate": result.model_dump(mode="json"),
                "reports": [r.model_dump(mode="json") for r in reports],
                "metrics": metrics.model_dump(mode="json"),
                "replay_live_calls": invoke.call_count,
                "external_network_attempts": len(network_guard.rejected),
                "real_environment_verified": False,
                "production_writes_enabled": False,
            },
        )
        assert result.passed, result.reasons
        assert network_guard.rejected == []
        print(
            f"本地 Fake Replay 基准：{result.hits}/{result.total}，"
            f"门槛 {criteria.rca_hit_rate:.0%}；真实能力待验收",
            flush=True,
        )
    finally:
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 58 本地 Replay 验收清理")
