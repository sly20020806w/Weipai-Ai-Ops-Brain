"""隔离本机 PostgreSQL/Temporal 的风险、证据、通知、恢复和 Replay 验收。"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.inspection.fake import FakeInspectionConnector, sample_facts
from app.db.base import utc_now
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.inspection.activities import InspectionActivities
from app.tasks.inspection.models import (
    InspectionReport,
    InspectionRequest,
    InspectionVerifyRequest,
    RiskEntry,
)
from app.tasks.inspection.workflow import InspectionWorkflow
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus, TransitionActor, VerificationRequired
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanResponse, TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.inspection import register_inspection_tools
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.registry import ToolRegistry, json_object
from app.triggers.activities import EventActivities
from app.triggers.schemas import EventBatch, NormalizedEvent
from app.triggers.service import EventService
from app.triggers.workflow import EventIngestionWorkflow
from app.verifier.inspection import InspectionVerifier
from tests.test_reviewer_integration import database, migrated_schema
from tests.test_workflow_integration import wait_at

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="运行 check-inspections.ps1 使用独立本机依赖",
    ),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


def settings(service: str) -> Settings:
    return Settings(APP_ENV="test", INSPECTION_CONFIG={"services": [service]})


async def new_scan(database: Database, service: str) -> InspectionRequest:
    async with database.session() as session, session.begin():
        receipt = (
            await EventService(session).accept(
                [
                    NormalizedEvent(
                        origin="schedule",
                        source=TaskSource.SCHEDULE,
                        external_id=f"workday-inspection:{uuid4()}",
                        service_name=service,
                        title="开工巡检",
                        occurred_at=utc_now(),
                    )
                ]
            )
        )[0]
        task = await session.get(AITask, UUID(receipt.task_id))
        assert task is not None
        for phase in (
            TaskStatus.CONTEXT_BUILDING,
            TaskStatus.RUNBOOK_MATCHING,
            TaskStatus.INVESTIGATING,
        ):
            task = await TaskService(session).transition(
                task.id,
                phase,
                expected_status=task.status,
                expected_version=task.status_version,
                reason="巡检测试",
            )
        return InspectionRequest(
            TaskSnapshot(str(task.id), task.status, task.status_version), "inspection"
        )


async def risks(database: Database, service: str) -> list[RiskEntry]:
    async with database.session() as session:
        return list(
            await session.scalars(select(RiskEntry).where(RiskEntry.service_name == service))
        )


async def verifying(database: Database, request: InspectionRequest) -> TaskSnapshot:
    async with database.session() as session, session.begin():
        task = await session.get(AITask, UUID(request.task.task_id))
        assert task is not None
        for phase in (
            TaskStatus.RCA,
            TaskStatus.PLANNING,
            TaskStatus.EXECUTING,
            TaskStatus.VERIFYING,
        ):
            task = await TaskService(session).transition(
                task.id,
                phase,
                expected_status=task.status,
                expected_version=task.status_version,
                reason="巡检只读完成",
            )
        return TaskSnapshot(str(task.id), task.status, task.status_version)


async def test_four_risks_real_evidence_notifications_and_retry_cache(database: Database) -> None:
    service = f"inspection-{uuid4().hex}"
    request = await new_scan(database, service)
    connectors: list[FakeInspectionConnector] = []

    def factory() -> FakeInspectionConnector:
        connector = FakeInspectionConnector(sample_facts(service))
        connectors.append(connector)
        return connector

    feishu = FakeFeishuConnector()
    activities = InspectionActivities(
        database, settings(service), connector_factory=factory, feishu=feishu
    )
    result = await activities.scan(request)
    assert await activities.scan(request) == result
    assert sum(c.calls for c in connectors) == 1
    rows = await risks(database, service)
    assert len(rows) == 4 and all(r.active for r in rows)
    assert {r.check_id for r in rows} == {
        "pdb_present",
        "hpa_present",
        "certificate_days",
        "ecs_idle",
    }
    for risk_id in result.risk_ids * 2:
        await activities.notify(risk_id)
    assert len(feishu.sent_messages) == 4
    async with database.session() as session:
        ledger = LedgerService(session)
        report = await ledger.get_evidence(UUID(result.evidence_id))
        checked = InspectionReport.model_validate_json(json.dumps(report.result_snapshot))
        assert checked.complete and len({c.area for c in checked.checks}) == 17
        assert len(checked.runbook_evidence_ids) == 1
        for row in rows:
            evidence = await ledger.get_evidence(row.opening_evidence_id)
            assert evidence.task_id == UUID(request.task.task_id)
            assert isinstance(evidence.result_snapshot, dict)
            fact = await ledger.get_evidence(UUID(str(evidence.result_snapshot["evidence_id"])))
            assert fact.source_tool == "query_inspection_facts"
        audits = await ledger.audits_for_task(UUID(request.task.task_id))
        assert sum(a.operation == "query_inspection_facts" for a in audits) == 1
        assert sum(a.operation == "inspection.notify" for a in audits) == 4
        assert not any(a.event_type is AuditEventType.EXECUTION for a in audits)


async def test_parallel_scans_and_distinct_tasks_do_not_duplicate_active_risks(
    database: Database,
) -> None:
    service = f"inspection-{uuid4().hex}"
    request = await new_scan(database, service)
    snapshot = sample_facts(service)
    activities = InspectionActivities(
        database, settings(service), connector_factory=lambda: FakeInspectionConnector(snapshot)
    )
    results = await asyncio.gather(*(activities.scan(request) for _ in range(3)))
    assert len({r.evidence_id for r in results}) == 1
    other = await new_scan(database, service)
    await activities.scan(other)
    rows = await risks(database, service)
    assert len(rows) == 4 and all(r.episode == 1 for r in rows)


async def test_healthy_silent_repeated_anomaly_clear_and_reopen(database: Database) -> None:
    service = f"inspection-{uuid4().hex}"
    feishu = FakeFeishuConnector()
    current = sample_facts(service, abnormal=False)
    activities = InspectionActivities(
        database,
        settings(service),
        connector_factory=lambda: FakeInspectionConnector(current),
        feishu=feishu,
    )
    healthy = await activities.scan(await new_scan(database, service))
    assert healthy.risk_ids == [] and await risks(database, service) == []
    assert feishu.sent_messages == ()
    current = sample_facts(service)
    abnormal = await activities.scan(await new_scan(database, service))
    for risk_id in abnormal.risk_ids:
        await activities.notify(risk_id)
    current = sample_facts(service)
    repeated = await activities.scan(await new_scan(database, service))
    for risk_id in repeated.risk_ids:
        await activities.notify(risk_id)
    assert len(feishu.sent_messages) == 4
    current = sample_facts(service, abnormal=False)
    # 闲置资源仍在读回范围，明确观测为非闲置；资源消失本身不等于风险恢复。
    current = current.model_copy(
        update={
            "facts": tuple(
                f.model_copy(update={"resource": "ecs-unused"}) if f.check_id == "ecs_idle" else f
                for f in current.facts
            )
        }
    )
    await activities.scan(await new_scan(database, service))
    assert not any(r.active for r in await risks(database, service))
    current = sample_facts(service)
    reopened = await activities.scan(await new_scan(database, service))
    for risk_id in reopened.risk_ids:
        await activities.notify(risk_id)
    assert len(feishu.sent_messages) == 8
    assert all(r.episode == 2 for r in await risks(database, service))


async def test_incomplete_stale_and_out_of_order_health_never_clear_risks(
    database: Database,
) -> None:
    service = f"inspection-{uuid4().hex}"
    current = sample_facts(service)
    activities = InspectionActivities(
        database, settings(service), connector_factory=lambda: FakeInspectionConnector(current)
    )
    await activities.scan(await new_scan(database, service))
    for invalid in ("stale", "incomplete"):
        current = sample_facts(service, abnormal=False)
        current = current.model_copy(
            update={
                "complete": invalid != "incomplete",
                "facts": tuple(
                    f.model_copy(update={"observed_at": utc_now() - timedelta(days=1)})
                    for f in current.facts
                )
                if invalid == "stale"
                else current.facts,
            }
        )
        result = await activities.scan(await new_scan(database, service))
        assert not InspectionReport.model_validate_json(result.report_json).complete
        assert (
            sum(
                r.active
                for r in await risks(database, service)
                if r.check_id in {"pdb_present", "hpa_present", "certificate_days", "ecs_idle"}
            )
            == 4
        )


async def test_policy_deny_has_audit_unknown_and_no_connector_calls(database: Database) -> None:
    service = f"inspection-{uuid4().hex}"
    request = await new_scan(database, service)
    connector = FakeInspectionConnector(sample_facts(service))
    configured = settings(service)
    from app.policy.models import PolicyConfig, PolicyDecision, PolicyRule, RiskLevel

    configured = configured.model_copy(
        update={
            "policy_config": PolicyConfig(
                rules=(
                    PolicyRule(
                        id="deny-inspection",
                        action_names=("query_inspection_facts",),
                        risk_levels=(RiskLevel.L0,),
                        decision=PolicyDecision.DENY,
                        reason="测试拒绝巡检事实查询",
                    ),
                )
            )
        }
    )
    result = await InspectionActivities(
        database, configured, connector_factory=lambda: connector
    ).scan(request)
    assert connector.calls == 0
    assert all(
        c.outcome == "unknown"
        for c in InspectionReport.model_validate_json(result.report_json).checks
    )
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(request.task.task_id))
        assert any(
            a.operation == "query_inspection_facts" and a.outcome == "rejected" for a in audits
        )


async def test_failure_rolls_back_all_scan_artifacts(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = f"inspection-{uuid4().hex}"
    request = await new_scan(database, service)
    original = LedgerService.append_evidence

    async def fail(self: LedgerService, **kwargs: object) -> Evidence:
        if kwargs.get("source_tool") == "inspection.report":
            raise RuntimeError("报告提交失败")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(RuntimeError, match="报告提交"):
        await InspectionActivities(database, settings(service)).scan(request)
    assert await risks(database, service) == []
    async with database.session() as session:
        assert await LedgerService(session).evidence_for_task(UUID(request.task.task_id)) == []


async def test_dispatcher_replay_uses_original_facts_without_connector(database: Database) -> None:
    service = f"inspection-{uuid4().hex}"
    request = await new_scan(database, service)
    result = await InspectionActivities(database, settings(service)).scan(request)
    report = InspectionReport.model_validate_json(result.report_json)
    connector = FakeInspectionConnector()
    await connector.aclose()
    registry = ToolRegistry()
    register_inspection_tools(registry, connector)
    async with database.session() as session, session.begin():
        replay = await ToolDispatcher(
            registry, create_policy_engine(settings(service)), LedgerService(session)
        ).dispatch(
            task_id=UUID(request.task.task_id),
            tool_name="query_inspection_facts",
            parameters={"service_name": service},
            actor="replay",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=report.checks[0].evidence_id,
            replay_before=utc_now(),
        )
        assert replay.status is DispatchStatus.REPLAYED
        assert replay.evidence_id == report.checks[0].evidence_id and connector.calls == 0


async def test_only_independent_verifier_resolves_scan_and_keeps_risks_open(
    database: Database,
) -> None:
    service = f"inspection-{uuid4().hex}"
    request = await new_scan(database, service)
    configured = settings(service)
    result = await InspectionActivities(database, configured).scan(request)
    task = await verifying(database, request)
    with pytest.raises(VerificationRequired):
        async with database.session() as session, session.begin():
            await TaskService(session).transition(
                UUID(task.task_id),
                TaskStatus.RESOLVED,
                expected_status=task.status,
                expected_version=task.version,
                actor=TransitionActor.VERIFIER,
                reason="伪造身份",
            )
    verification = InspectionVerifyRequest(task, result.evidence_id)
    verifier = InspectionVerifier(database, configured)
    simultaneous = await asyncio.gather(*(verifier.verify(verification) for _ in range(3)))
    assert len(set(simultaneous)) == 1
    checked = simultaneous[0]
    assert checked.status is TaskStatus.RESOLVED
    assert await verifier.verify(verification) == checked
    assert all(r.active for r in await risks(database, service))


async def test_tampered_report_and_changed_rules_cannot_resolve(database: Database) -> None:
    service = f"inspection-{uuid4().hex}"
    request = await new_scan(database, service)
    configured = settings(service)
    result = await InspectionActivities(database, configured).scan(request)
    task = await verifying(database, request)
    report = InspectionReport.model_validate_json(result.report_json)
    tampered = report.model_copy(
        update={
            "checks": (
                report.checks[0].model_copy(update={"evidence_id": uuid4()}),
                *report.checks[1:],
            )
        }
    )
    async with database.session() as session, session.begin():
        forged = await LedgerService(session).append_evidence(
            task_id=UUID(task.task_id),
            source_tool="inspection.report",
            parameters={},
            result_snapshot=json_object(tampered.model_dump(mode="json")),
        )
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError):
        await InspectionVerifier(database, configured).verify(
            InspectionVerifyRequest(task, str(forged.id))
        )
    changed = Settings(
        APP_ENV="test",
        INSPECTION_CONFIG={"services": [service], "thresholds": {"certificate_days": 5}},
    )
    with pytest.raises(ApplicationError):
        await InspectionVerifier(database, changed).verify(
            InspectionVerifyRequest(task, result.evidence_id)
        )


@local_temporal
async def test_periodic_event_unified_task_child_workflow_closed_and_history_replay(
    database: Database,
) -> None:
    service = f"inspection-{uuid4().hex}"
    queue = f"inspection-test-{uuid4().hex}"
    configured = settings(service).model_copy(
        update={
            "temporal_config": settings(service).temporal_config.model_copy(
                update={
                    "address": os.environ["TEST_TEMPORAL_ADDRESS"],
                    "task_queue": queue,
                }
            )
        }
    )
    client = await Client.connect(
        os.environ["TEST_TEMPORAL_ADDRESS"],
        namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default"),
    )
    feishu = FakeFeishuConnector()
    async with create_worker(client, database, configured, feishu_connector=feishu):
        event = NormalizedEvent(
            origin="schedule",
            source=TaskSource.SCHEDULE,
            external_id=f"workday-inspection:{uuid4()}",
            service_name=service,
            title="开工巡检",
            occurred_at=utc_now(),
        )
        ingest = await client.start_workflow(
            EventIngestionWorkflow.run,
            EventBatch([event.model_dump_json()]),
            id=f"inspection-ingest-{uuid4().hex}",
            task_queue=queue,
        )
        receipt = (await ingest.result())[0]
        handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
        progress = await asyncio.wait_for(handle.result(), timeout=60)
        assert progress.task and progress.task.status is TaskStatus.CLOSED
        assert len(feishu.sent_messages) == 4
        assert len(await risks(database, service)) == 4
        async with database.session() as session:
            history = await TaskService(session).history(UUID(receipt.task_id))
            assert [h.to_status for h in history] == [s.status for s in progress.history]
        await EventActivities(database, configured, client).start_task(
            replace(receipt, duplicate=True)
        )
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
        child = client.get_workflow_handle_for(
            InspectionWorkflow.run, f"{receipt.workflow_id}/inspection/3"
        )
        await Replayer(workflows=[InspectionWorkflow]).replay_workflow(await child.fetch_history())
        assert len(feishu.sent_messages) == 4


async def test_sent_notification_transaction_failure_retries_same_message(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = f"inspection-{uuid4().hex}"
    feishu = FakeFeishuConnector()
    activities = InspectionActivities(database, settings(service), feishu=feishu)
    result = await activities.scan(await new_scan(database, service))
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> object:
        if kwargs.get("operation") == "inspection.notify":
            raise RuntimeError("通知审计提交失败")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError, match="通知审计"):
        await activities.notify(result.risk_ids[0])
    assert len(feishu.sent_messages) == 1
    monkeypatch.setattr(LedgerService, "append_audit", original)
    await activities.notify(result.risk_ids[0])
    assert len(feishu.sent_messages) == 1
    row = next(r for r in await risks(database, service) if str(r.id) == result.risk_ids[0])
    assert row.notification_evidence_id is not None


async def test_future_source_time_does_not_poison_risk_observation_or_recovery(
    database: Database,
) -> None:
    service = f"inspection-{uuid4().hex}"
    current = sample_facts(service, abnormal=False)
    current = current.model_copy(
        update={
            "facts": tuple(
                f.model_copy(update={"observed_at": utc_now() + timedelta(days=365)})
                for f in current.facts
            )
        }
    )
    activities = InspectionActivities(
        database, settings(service), connector_factory=lambda: FakeInspectionConnector(current)
    )
    future = await activities.scan(await new_scan(database, service))
    assert not InspectionReport.model_validate_json(future.report_json).complete
    assert all(
        r.first_seen <= utc_now() and r.last_seen <= utc_now()
        for r in await risks(database, service)
    )
    current = sample_facts(service, abnormal=False)
    healthy = await activities.scan(await new_scan(database, service))
    assert InspectionReport.model_validate_json(healthy.report_json).complete
    assert not any(r.active for r in await risks(database, service))


@local_temporal
async def test_missing_data_wait_survives_worker_restart_then_rescans(database: Database) -> None:
    service = f"inspection-{uuid4().hex}"
    queue = f"inspection-restart-{uuid4().hex}"
    configured = Settings(
        APP_ENV="test",
        INSPECTION_CONFIG={"services": [service]},
        TEMPORAL_CONFIG={"address": os.environ["TEST_TEMPORAL_ADDRESS"], "task_queue": queue},
    )
    client = await Client.connect(
        os.environ["TEST_TEMPORAL_ADDRESS"],
        namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default"),
    )
    feishu = FakeFeishuConnector()
    current = sample_facts(service, abnormal=False)
    current = current.model_copy(
        update={"facts": tuple(f for f in current.facts if f.check_id != "ecs_idle")}
    )
    activities = InspectionActivities(
        database,
        configured,
        connector_factory=lambda: FakeInspectionConnector(current),
        feishu=feishu,
    )
    async with database.session() as session, session.begin():
        receipt = (
            await EventService(session).accept(
                [
                    NormalizedEvent(
                        origin="schedule",
                        source=TaskSource.SCHEDULE,
                        external_id=f"workday-inspection:{uuid4()}",
                        service_name=service,
                        title="开工巡检",
                        occurred_at=utc_now(),
                    )
                ]
            )
        )[0]
    handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
    try:
        async with create_worker(client, database, configured, inspection_activities=activities):
            await EventActivities(database, configured, client).start_task(receipt)
            waiting = await wait_at(handle, TaskStatus.WAITING_INFORMATION)
            assert len(feishu.sent_messages) == 1
            await handle.signal(
                AITaskWorkflow.human_response,
                HumanResponse(TaskStatus.WAITING_INFORMATION, waiting.version - 1, True),
            )
            assert (await handle.query(AITaskWorkflow.progress)).task == waiting
        current = sample_facts(service, abnormal=False)
        async with create_worker(
            client, database, configured, max_cached_workflows=0, inspection_activities=activities
        ):
            await handle.signal(
                AITaskWorkflow.human_response,
                HumanResponse(TaskStatus.WAITING_INFORMATION, waiting.version, True),
            )
            progress = await asyncio.wait_for(handle.result(), timeout=60)
            assert progress.task and progress.task.status is TaskStatus.CLOSED
            assert not any(r.active for r in await risks(database, service))
            assert len(feishu.sent_messages) == 1
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        from temporalio.client import WorkflowExecutionStatus

        if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("巡检隔离恢复测试结束")
