"""Step 54：签名告警→真实 Temporal/数据库→审批 API→Fake 回滚→验证→学习。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from functools import partial
from typing import cast
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer

from app.agent.investigation import AgentConclusion
from app.agent.reviewer.models import AlternativeCause, ReviewDecision
from app.api.console import get_control_gateway
from app.api.events import get_event_gateway
from app.api.main import create_app
from app.config import Settings, parse_database_url
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector
from app.connectors.observability import fake as observability_fake
from app.connectors.observability.fake import SAMPLE_END
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.learning.models import SECTIONS, IncidentReport
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.models import PolicyDecision, RiskLevel
from app.runbooks.models import Runbook
from app.runbooks.schemas import AutomationLevel, RunbookMaturity
from app.tasks.approval.models import action_hash
from app.tasks.control import ControlGateway
from app.tasks.control_workflow import TaskControlWorkflow
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus, TransitionActor
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import WorkflowProgress
from app.tools.observability import MetricsOutput
from app.tools.timeline import RecentChangesOutput
from app.tools.verification_runtime import fake_verification_registry
from app.triggers.gateway import EventGateway
from app.triggers.models import OpsEvent
from app.triggers.workflow import EventIngestionWorkflow
from app.verifier.activities import VerifierActivities
from app.verifier.models import ResourceExpectation, VerificationReport
from tests.auth_support import PASSWORD, auth_config
from tests.database_support import get_test_database_url, migrate
from tests.e2e_support import NetworkGuard, require_local_temporal
from tests.test_approval_integration import wait_prompt
from tests.test_events import alert_body, signed

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("TEST_TEMPORAL_ADDRESS"),
        reason="check.ps1 的强制 E2E 阶段或 check-e2e.ps1 提供本机隔离依赖",
    ),
]

STATES = [
    TaskStatus.NEW,
    TaskStatus.CONTEXT_BUILDING,
    TaskStatus.RUNBOOK_MATCHING,
    TaskStatus.INVESTIGATING,
    TaskStatus.RCA,
    TaskStatus.PLANNING,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.EXECUTING,
    TaskStatus.VERIFYING,
    TaskStatus.RESOLVED,
    TaskStatus.LEARNING,
    TaskStatus.CLOSED,
]
ALERT_AT = SAMPLE_END + timedelta(minutes=30)


@pytest_asyncio.fixture
async def network_guard() -> AsyncIterator[NetworkGuard]:
    url = parse_database_url(get_test_database_url())
    address = os.environ["TEST_TEMPORAL_ADDRESS"]
    port = require_local_temporal(address)
    assert url.host == "127.0.0.1" and url.port
    guard = NetworkGuard(frozenset({("127.0.0.1", url.port), ("127.0.0.1", port)}))
    original = Client.connect

    async def connect(cls: type[Client], /, target_host: str, **kwargs: object) -> Client:
        # Temporal 使用 Rust transport，Python socket 补丁不能拦截它，入口须单独限域。
        if target_host != address:
            guard.rejected.append("Temporal")
            raise AssertionError("E2E Temporal 只能连接已验证的本机端口")
        return await original(target_host, **kwargs)  # type: ignore[arg-type]

    # Windows event loop 的 socketpair 先由 pytest 初始化；仅在业务场景内安装门禁。
    with pytest.MonkeyPatch.context() as monkeypatch:
        guard.install(monkeypatch)
        monkeypatch.setattr(Client, "connect", classmethod(connect))
        yield guard
        assert guard.rejected == [], "业务曾尝试真实联网，即使捕获异常也不能通过 E2E"


@pytest_asyncio.fixture
async def database(network_guard: NetworkGuard) -> AsyncIterator[Database]:
    migrate("upgrade", "head")
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest.mark.parametrize(
    "decision,recovered",
    [("approved", True), ("rejected", True), ("approved", False)],
    ids=["closed-loop", "approval-rejected", "verification-unrecovered"],
)
async def test_payment_alert_closed_loop(
    database: Database,
    network_guard: NetworkGuard,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    recovered: bool,
) -> None:
    # 本步专用样例：发布 01:25，故障采样 01:30–01:40，回滚/恢复从 01:40 开始。
    monkeypatch.setattr(observability_fake, "SAMPLE_START", ALERT_AT - timedelta(minutes=10))
    monkeypatch.setattr(observability_fake, "SAMPLE_END", ALERT_AT)
    settings = Settings(
        APP_ENV="test",
        AUTH_CONFIG=auth_config(),
        AGENT_CONFIG={"enabled": True},
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
        TRIGGER_CONFIG={
            "webhook_secrets": {"prometheus": "offline-test-signature-key-32-characters"}
        },
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"e2e-{uuid4().hex}",
            "human_timeout_seconds": 120,
        },
    )
    assert settings.connector_mode.value == settings.llm_mode == "fake"
    await DiscoveryActivities(database, settings).refresh(
        DiscoveryRequest(ALERT_AT.isoformat(), 3600)
    )
    await TimelineActivities(database, settings).collect(
        TimelineRequest(
            "payment-service", (ALERT_AT - timedelta(hours=1)).isoformat(), ALERT_AT.isoformat()
        )
    )
    connector = FakeKubernetesWriteConnector(clock=lambda: ALERT_AT)
    assert connector.targets["payment-service"].image.endswith(":v2.3.7")
    feishu = FakeFeishuConnector()
    verifier = VerifierActivities(
        database,
        settings,
        registry_factory=partial(
            fake_verification_registry,
            recovered=recovered,
            window_start=ALERT_AT,
            window_end=ALERT_AT + timedelta(minutes=5),
        ),
    )
    client = await Client.connect(settings.temporal_config.address)
    app = create_app(settings)
    app.state.database = database
    app.dependency_overrides[get_event_gateway] = lambda: EventGateway(client, settings)
    app.dependency_overrides[get_control_gateway] = lambda: ControlGateway(
        client, settings, database
    )
    body_data = json.loads(alert_body(starts=ALERT_AT.isoformat()))
    body_data["alerts"][0]["labels"]["e2e_case"] = uuid4().hex
    body = json.dumps(body_data).encode()
    task_id: UUID | None = None
    handles = []
    try:
        async with (
            create_worker(
                client,
                database,
                settings,
                feishu_connector=feishu,
                executor_activities=ExecutorActivities(database, settings, connector=connector),
                verifier_activities=verifier,
            ),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as http,
        ):
            assert (await http.post("/webhooks/prometheus", content=body)).status_code == 401
            response = await http.post("/webhooks/prometheus", content=body, headers=signed(body))
            assert response.status_code == 202, response.text
            receipt = response.json()["events"][0]
            task_id = UUID(receipt["task_id"])
            handle = client.get_workflow_handle(
                receipt["workflow_id"], result_type=WorkflowProgress
            )
            handles.append(handle)
            prompt = await wait_prompt(handle)
            assert prompt.task.status is TaskStatus.WAITING_APPROVAL
            assert connector.issue_count == connector.execution_count == 0
            assert any(
                prompt.action_hash in json.dumps(m.notification.model_dump(mode="json"))
                for m in feishu.sent_messages
            )
            async with database.session() as session:
                event = await session.get(OpsEvent, UUID(receipt["event_id"]))
                assert event and event.origin == "prometheus" and event.task_id == task_id
                assert event.source == TaskSource.ALERT.value and event.occurred_at.tzinfo is UTC
                task = await session.get(AITask, task_id)
                assert (
                    task
                    and task.source is TaskSource.ALERT
                    and task.status is TaskStatus.WAITING_APPROVAL
                )
                evidence = await LedgerService(session).evidence_for_task(task_id)
                by_id = {e.id: e for e in evidence}
                pending = await handle.query(AITaskWorkflow.progress)
                assert pending.conclusion_json and pending.review_json and pending.action_plan_json
                conclusion = AgentConclusion.model_validate_json(pending.conclusion_json)
                review = ReviewDecision.model_validate_json(pending.review_json)
                plan = ActionPlan.model_validate_json(pending.action_plan_json)
                assert (
                    "连接" in conclusion.root_cause.statement
                    and conclusion.evidence_ids <= by_id.keys()
                )
                assert review.report.verdict == "clear" and {
                    c.alternative for c in review.report.checks
                } == set(AlternativeCause)
                assert review.report.evidence_ids <= by_id.keys()
                assert plan.conclusion_evidence_id == UUID(pending.conclusion_evidence_id or "")
                assert plan.review_evidence_id == UUID(pending.review_evidence_id or "")
                assert (
                    action_hash(plan) == prompt.action_hash
                    and plan.decision is PolicyDecision.NEED_APPROVAL
                )
                action = plan.actions[0].action
                assert len(plan.actions) == 1 and action.name == "rollback_prod"
                assert (
                    action.service_name == "payment-service" and action.risk_level is RiskLevel.L3
                )
                assert action.parameters == {"from_version": "v2.3.7", "to_version": "v2.3.6"}
                assert action.rollback.description and action.verification.checks
                changes = next(e for e in evidence if e.source_tool == "get_recent_changes")
                assert "v2.3.7" in json.dumps(changes.result_snapshot)
                changed = RecentChangesOutput.model_validate_json(
                    json.dumps(changes.result_snapshot)
                )
                deployed = [e for e in changed.events if e.kind == "Deploy"]
                metrics_record = next(e for e in evidence if e.source_tool == "query_metrics")
                metrics = MetricsOutput.model_validate_json(
                    json.dumps(metrics_record.result_snapshot)
                )
                points = [p for series in metrics.series for p in series.points]
                assert deployed and points
                assert max(e.occurred_at for e in deployed) < min(p.timestamp for p in points)
                assert max(p.timestamp for p in points) <= ALERT_AT
                assert {
                    "search_runbooks",
                    "get_service_context",
                    "get_recent_changes",
                    "query_metrics",
                    "query_logs",
                } <= {e.source_tool for e in evidence}
            duplicate = await http.post("/webhooks/prometheus", content=body, headers=signed(body))
            assert duplicate.status_code == 202 and duplicate.json()["events"][0]["duplicate"]
            assert duplicate.json()["events"][0]["task_id"] == str(task_id)
            assert duplicate.json()["events"][0]["event_id"] == receipt["event_id"]
            print(
                f"\nRCA Evidence：{pending.conclusion_evidence_id} / "
                f"Reviewer：{pending.review_evidence_id} / 计划：{pending.action_plan_evidence_id}"
            )
            assert (await http.get(f"/api/tasks/{task_id}")).status_code == 401
            login = await http.post(
                "/api/auth/login",
                headers={"X-Ops-Login": "1"},
                json={"username": "local-owner", "password": PASSWORD},
            )
            assert login.status_code == 200
            http.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            approval = {
                "approval_id": prompt.approval_id,
                "wait_version": prompt.task.version,
                "action_hash": prompt.action_hash,
                "decision": decision,
            }
            path = f"/api/tasks/{task_id}/approval"
            assert (
                await http.post(path, json={**approval, "action_hash": "0" * 64})
            ).status_code == 409
            assert connector.issue_count == connector.execution_count == 0
            approved = await http.post(path, json=approval)
            assert approved.status_code == 202, approved.text
            operation = client.get_workflow_handle(approved.json()["operation_id"])
            handles.append(operation)
            result = await asyncio.wait_for(handle.result(), 60)
            assert result.task
            again = await http.post(path, json=approval)
            assert again.status_code == 202 and again.json() == approved.json()
            expected = (
                STATES
                if decision == "approved" and recovered
                else (
                    STATES[:7] + [TaskStatus.ESCALATED]
                    if decision == "rejected"
                    else STATES[:9] + [result.task.status]
                )
            )
            async with database.session() as session:
                ledger = LedgerService(session)
                history = await TaskService(session).history(task_id)
                evidence = await ledger.evidence_for_task(task_id)
                audits = await ledger.audits_for_task(task_id)
                assert [h.to_status for h in history] == expected
                assert [(h.to_status, h.sequence) for h in history] == [
                    (h.status, h.version) for h in result.history
                ]
                assert [h.from_status for h in history] == [None, *expected[:-1]]
                assert all(h.reason and h.changed_at.tzinfo is UTC for h in history)
                transitions = sorted(
                    (a for a in audits if a.event_type is AuditEventType.STATE_TRANSITION),
                    key=lambda a: cast(int, a.details["status_version"]),
                )
                assert len(transitions) == len(history)
                for h, audit in zip(history, transitions, strict=True):
                    assert audit.details == {
                        "from_status": h.from_status.value if h.from_status else None,
                        "to_status": h.to_status.value,
                        "status_version": h.sequence,
                        "reason": h.reason,
                    }
                    assert audit.actor == h.actor.value and audit.occurred_at == h.changed_at
                decisions = [a for a in audits if a.operation == "approval.decide"]
                assert len(decisions) == 1 and decisions[0].actor == "local-owner"
                assert decisions[0].outcome == decision and decisions[0].occurred_at.tzinfo is UTC
                by_id = {e.id: e for e in evidence}
                assert all(e.task_id == task_id and e.collected_at.tzinfo is UTC for e in evidence)
                assert all(a.evidence_id is None or a.evidence_id in by_id for a in audits)
                calls = [
                    a
                    for a in audits
                    if a.event_type is AuditEventType.TOOL_CALL and a.outcome == "succeeded"
                ]
                for audit in calls:
                    assert (
                        audit.evidence_id
                        and by_id[audit.evidence_id].source_tool == audit.operation
                    )
                    assert sum(a.evidence_id == audit.evidence_id for a in calls) == 1
                execution_audits = [
                    a
                    for a in audits
                    if a.event_type is AuditEventType.EXECUTION and a.outcome == "succeeded"
                ]
                if decision == "rejected":
                    assert result.task.status is TaskStatus.ESCALATED
                    assert (
                        connector.issue_count
                        == connector.execution_count
                        == len(execution_audits)
                        == 0
                    )
                    assert result.verification_json is None
                else:
                    assert (
                        connector.issue_count
                        == connector.execution_count
                        == len(execution_audits)
                        == 1
                    )
                    assert connector.targets["payment-service"].image.endswith(":v2.3.6")
                    assert result.verification_json and result.verification_evidence_id
                    verified = VerificationReport.model_validate_json(result.verification_json)
                    assert (
                        verified.passed is recovered
                        and verified.spec.expected_image
                        == connector.targets["payment-service"].image
                    )
                    assert (
                        verified.spec.action_completed_at
                        == next(iter(connector.receipts.values())).completed_at
                    )
                    assert all(check.evidence_id in by_id for check in verified.checks)
                    assert UUID(result.verification_evidence_id) in by_id
                if decision == "approved" and recovered:
                    assert (
                        result.task.status is TaskStatus.CLOSED
                        and result.postmortem_json
                        and result.postmortem_evidence_id
                    )
                    assert history[-3].actor is TransitionActor.VERIFIER
                    report = IncidentReport.model_validate_json(result.postmortem_json)
                    assert tuple(s.title for s in report.sections) == SECTIONS
                    assert report.evidence_ids <= by_id.keys()
                    assert {i.evidence_id for i in report.timeline} <= by_id.keys()
                    assert UUID(result.postmortem_evidence_id) in by_id
                    assert sum(e.source_tool == "postmortem" for e in evidence) == 1
                    draft = await session.get(Runbook, report.runbook_id)
                    assert draft and draft.maturity == RunbookMaturity.DRAFT.value
                    assert draft.automation_level == AutomationLevel.MANUAL.value
                    assert draft.success_count == draft.failure_count == 0
                    assert report.improvement_task_ids
                    for child_id in report.improvement_task_ids:
                        child = await session.get(AITask, child_id)
                        origin = await session.scalar(
                            select(OpsEvent).where(OpsEvent.task_id == child_id)
                        )
                        assert child and child.source is TaskSource.AI
                        assert origin and origin.origin == "learning"
                        child_handle = client.get_workflow_handle(f"ai-task-{child_id}")
                        handles.append(child_handle)
                        assert (
                            await child_handle.describe()
                        ).status is WorkflowExecutionStatus.RUNNING
                    for entry in evidence:
                        response = await http.get(f"/api/evidence/{entry.id}")
                        assert (
                            response.status_code == 200
                            and response.json()["result_snapshot"] == entry.result_snapshot
                        )
                    print(
                        f"\n复盘十三章 / Draft Runbook：{draft.id} / "
                        f"改进任务：{len(report.improvement_task_ids)}"
                    )
                else:
                    assert result.task.status not in {TaskStatus.RESOLVED, TaskStatus.CLOSED}
                    assert result.postmortem_json is result.postmortem_evidence_id is None
                    assert not any(e.source_tool.startswith("postmortem") for e in evidence)
                    if decision == "approved":
                        assert result.task.status in {
                            TaskStatus.INVESTIGATING,
                            TaskStatus.AUTOMATION_ABORTED,
                        }
                print(f"\n任务：{task_id} / Workflow：{handle.id}")
                print("状态：" + " → ".join(h.to_status.value for h in history))
                print(
                    f"Evidence：{len(evidence)} / 审计：{len(audits)} / "
                    f"Fake 回滚：{connector.execution_count} / "
                    f"真实运维 HTTP 与外网尝试：{len(network_guard.rejected)}"
                )
            await Replayer(
                workflows=[AITaskWorkflow, TaskControlWorkflow, EventIngestionWorkflow]
            ).replay_workflow(await handle.fetch_history())
            await Replayer(workflows=[TaskControlWorkflow]).replay_workflow(
                await operation.fetch_history()
            )
    finally:
        # 即使断言失败，也从已提交复盘找回改进任务，避免遗留等待 Workflow。
        if task_id is not None:
            async with database.session() as session:
                for entry in await LedgerService(session).evidence_for_task(task_id):
                    if entry.source_tool == "postmortem":
                        report = IncidentReport.model_validate_json(
                            json.dumps(entry.result_snapshot)
                        )
                        handles.extend(
                            client.get_workflow_handle(f"ai-task-{child_id}")
                            for child_id in report.improvement_task_ids
                        )
        for running in handles:
            if (await running.describe()).status is WorkflowExecutionStatus.RUNNING:
                await running.terminate("Step 54 隔离验收清理")
        await connector.aclose()
        await feishu.aclose()
