"""本机隔离 PostgreSQL/Temporal 验收；不连接外部运维系统。"""

import asyncio
import json
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from temporalio import activity
from temporalio.client import Client, ScheduleActionExecutionStartWorkflow
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.learning.automation.activities import AutomationActivities
from app.learning.automation.demo import seed_manual
from app.learning.automation.models import (
    AutomationConfig,
    AutomationInput,
    AutomationResult,
    ManualOperation,
    ScanRequest,
    ScanWindow,
    Suggestion,
    WorkKind,
    group_records,
)
from app.learning.automation.schedule import ensure_automation_schedule
from app.learning.automation.service import AutomationService
from app.learning.automation.sources import collect_records
from app.learning.automation.workflow import AutomationDiscoveryWorkflow
from app.learning.evaluation.demo import closed_fake_incident
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.ledger.service import LedgerService
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.triggers.activities import EventActivities
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventReceipt, NormalizedEvent
from app.triggers.service import EventService
from tests.test_reviewer_integration import database, migrated_schema
from tests.test_runbooks import sample_view
from tests.test_workflow_integration import wait_at

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="运行 check-automation.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


async def scan(
    database: Database, end: str | None = None, *, config: AutomationConfig | None = None
) -> AutomationResult:
    async with database.session() as session, session.begin():
        return await AutomationService(session).scan(
            config or AutomationConfig(), end or utc_now().isoformat()
        )


async def for_service(
    database: Database, result: AutomationResult, service: str
) -> list[EventReceipt]:
    async with database.session() as session:
        events = list(
            await session.scalars(select(OpsEvent).where(OpsEvent.service_name == service))
        )
    ids = {str(e.id) for e in events}
    return [r for r in result.receipts if r.event_id in ids]


async def test_five_manual_records_one_suggestion_four_silent_and_atomic_references(
    database: Database,
) -> None:
    service = f"automation-{uuid4().hex}"
    ids = await seed_manual(database, service, 4)
    assert await for_service(database, await scan(database), service) == []
    ids += await seed_manual(database, service, 1)
    result = await scan(database)
    receipts = await for_service(database, result, service)
    assert len(receipts) == 1 and not receipts[0].duplicate
    async with database.session() as session:
        task_id = UUID(receipts[0].task_id)
        task = await session.get(AITask, task_id)
        assert task and task.source is TaskSource.AI and task.status is TaskStatus.NEW
        evidence = await LedgerService(session).evidence_for_task(task_id)
        assert len(evidence) == 1
        suggestion = Suggestion.model_validate_json(json.dumps(evidence[0].result_snapshot))
        assert len(suggestion.repetition.records) == 5 and suggestion.method == "script"
        assert {str(r.evidence_id) for r in suggestion.repetition.records} == set(ids)
        for reference in suggestion.repetition.records:
            audit = await session.get(AuditRecord, reference.record_id)
            assert audit and audit.evidence_id == reference.evidence_id
            assert audit.task_id == reference.task_id and audit.actor == "demo-operator"
        assert len(await LedgerService(session).audits_for_task(task_id)) == 1  # 统一任务创建审计。
        history = list(
            await session.scalars(
                select(TaskStatusHistory).where(TaskStatusHistory.task_id == task_id)
            )
        )
        assert len(history) == 1 and history[0].to_status is TaskStatus.NEW


async def test_concurrent_scans_retries_and_new_records_keep_single_suggestion(
    database: Database,
) -> None:
    service = f"automation-{uuid4().hex}"
    await seed_manual(database, service, 5)
    cutoff = utc_now().isoformat()
    results = await asyncio.gather(*(scan(database, cutoff) for _ in range(4)))
    receipts = [r for result in results for r in await for_service(database, result, service)]
    assert len({r.task_id for r in receipts}) == 1
    assert sum(not r.duplicate for r in receipts) == 1
    await seed_manual(database, service, 1)
    later = await for_service(database, await scan(database), service)
    assert len(later) == 1 and later[0].duplicate and later[0].task_id == receipts[0].task_id
    async with database.session() as session:
        assert len(await LedgerService(session).evidence_for_task(UUID(later[0].task_id))) == 1


async def test_suggestion_failure_rolls_back_event_task_evidence_and_audit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = f"automation-{uuid4().hex}"
    await seed_manual(database, service, 5)
    original = LedgerService.append_evidence

    async def fail(self: LedgerService, **kwargs: object) -> Evidence:
        if kwargs.get("source_tool") == "automation.suggestion":
            raise RuntimeError("模拟证据写入失败")
        raise AssertionError("本测试只会写入建议证据")

    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(RuntimeError, match="模拟"):
        await scan(database)
    async with database.session() as session:
        assert (
            await session.scalar(
                select(OpsEvent).where(
                    OpsEvent.service_name == service, OpsEvent.origin == "learning"
                )
            )
            is None
        )
    monkeypatch.setattr(LedgerService, "append_evidence", original)
    assert len(await for_service(database, await scan(database), service)) == 1


async def test_manual_record_is_idempotent_and_rejects_conflict_future_and_wrong_service(
    database: Database,
) -> None:
    service = f"automation-{uuid4().hex}"
    ids = await seed_manual(database, service, 1)
    async with database.session() as session:
        previous = await LedgerService(session).get_evidence(UUID(ids[0]))
        value = ManualOperation.model_validate_json(json.dumps(previous.result_snapshot))
    async with database.session() as session, session.begin():
        assert await AutomationService(session).record_manual(value) == previous.id
    for invalid in (
        value.model_copy(update={"operation": "changed"}),
        value.model_copy(update={"occurred_at": utc_now() + timedelta(hours=1)}),
        value.model_copy(update={"service_name": "other-service"}),
        value.model_copy(update={"task_id": uuid4()}),
    ):
        with pytest.raises(ValueError):
            async with database.session() as session, session.begin():
                await AutomationService(session).record_manual(invalid)
    async with database.session() as session:
        assert len(await LedgerService(session).evidence_for_task(value.task_id)) == 1
        assert (
            len(
                [
                    a
                    for a in await LedgerService(session).audits_for_task(value.task_id)
                    if a.operation == "manual.operation"
                ]
            )
            == 1
        )


async def test_manual_audit_failure_is_atomic(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = f"automation-{uuid4().hex}"
    ids = await seed_manual(database, service, 1)
    async with database.session() as session:
        previous = await LedgerService(session).get_evidence(UUID(ids[0]))
        value = ManualOperation.model_validate_json(json.dumps(previous.result_snapshot))
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> AuditRecord:
        raise RuntimeError("模拟审计失败")

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError, match="审计"):
        async with database.session() as session, session.begin():
            await AutomationService(session).record_manual(
                value.model_copy(update={"record_key": "second"})
            )
    monkeypatch.setattr(LedgerService, "append_audit", original)
    async with database.session() as session:
        assert len(await LedgerService(session).evidence_for_task(value.task_id)) == 1


async def test_half_open_windows_late_evidence_replay_audit_and_learning_feedback_excluded(
    database: Database,
) -> None:
    service = f"automation-{uuid4().hex}"
    ids = await seed_manual(database, service, 1)
    async with database.session() as session, session.begin():
        previous = await LedgerService(session).get_evidence(UUID(ids[0]))
        await LedgerService(session).append_audit(
            task_id=previous.task_id,
            event_type=AuditEventType.HUMAN_INTERACTION,
            actor="replay:test",
            operation="manual.operation",
            outcome="recorded",
            evidence_id=previous.id,
            details={"mode": "replay"},
            occurred_at=previous.collected_at,
        )
        # 同一证据再记 live 审计也不能重复算劳动。
        await LedgerService(session).append_audit(
            task_id=previous.task_id,
            event_type=AuditEventType.HUMAN_INTERACTION,
            actor="demo-operator",
            operation="manual.operation",
            outcome="recorded",
            evidence_id=previous.id,
            details={},
            occurred_at=previous.collected_at,
        )
    async with database.session() as session:
        all_records = await collect_records(
            session, ScanWindow(start=previous.collected_at, end=utc_now())
        )
        assert len([r for r in all_records if r.service_name == service]) == 1
        outside = await collect_records(
            session,
            ScanWindow(start=previous.collected_at - timedelta(days=1), end=previous.collected_at),
        )
        assert not any(r.service_name == service for r in outside)
        # 截止点早于原记录入库，即使采集时间更早也不能纳入。
        late = await collect_records(
            session,
            ScanWindow(start=previous.collected_at - timedelta(days=1), end=previous.created_at),
        )
        assert not any(r.service_name == service for r in late)
    await seed_manual(database, service, 4)
    receipt = (await for_service(database, await scan(database), service))[0]
    with pytest.raises(ValueError, match="建议"):
        async with database.session() as session, session.begin():
            await AutomationService(session).record_manual(
                ManualOperation(
                    task_id=UUID(receipt.task_id),
                    record_key="loop",
                    service_name=service,
                    operation="self-feedback",
                    actor="operator",
                    source_reference="fake://loop",
                    occurred_at=utc_now(),
                )
            )


async def test_config_disabled_future_and_naive_end_rejected(database: Database) -> None:
    assert (await scan(database, config=AutomationConfig(enabled=False))).receipts == []
    for end in (
        utc_now().replace(tzinfo=None).isoformat(),
        (utc_now() + timedelta(days=1)).isoformat(),
    ):
        with pytest.raises(ValueError):
            await scan(database, end)


async def test_ticket_release_and_runbook_sources_group_real_database_records(
    database: Database,
) -> None:
    began = utc_now()
    service = f"automation-{uuid4().hex}"
    runbook = sample_view()
    async with database.session() as session, session.begin():
        for _index in range(5):
            for source, origin, prefix in (
                (TaskSource.TICKET, "ops_platform", "ticket"),
                (TaskSource.SCHEDULE, "schedule", "release-verification"),
            ):
                event = NormalizedEvent.model_validate(
                    {
                        "origin": origin,
                        "source": source,
                        "external_id": f"{prefix}:{uuid4()}",
                        "service_name": service,
                        "title": "申请同类支付权限",
                        "occurred_at": utc_now(),
                    }
                )
                receipt = (await EventService(session).accept([event]))[0]
                if source is TaskSource.TICKET:
                    task_id = UUID(receipt.task_id)
                    search = await LedgerService(session).append_evidence(
                        task_id=task_id,
                        source_tool="search_runbooks",
                        parameters={"query": service},
                        result_snapshot={"matches": []},
                    )
                    await LedgerService(session).append_audit(
                        task_id=task_id,
                        actor="codex-main-agent",
                        event_type=AuditEventType.TOOL_CALL,
                        operation="search_runbooks",
                        outcome="succeeded",
                        evidence_id=search.id,
                        details={"mode": "live"},
                    )
                    for phase in (1, 2):
                        await LedgerService(session).append_evidence(
                            task_id=task_id,
                            source_tool="runbook.match",
                            parameters={"phase_version": phase},
                            result_snapshot={
                                "runbook_json": runbook.model_dump_json(),
                                "blocked": False,
                                "search_evidence_id": str(search.id),
                            },
                        )
    async with database.session() as session:
        records = await collect_records(session, ScanWindow(start=began, end=utc_now()))
    relevant = tuple(r for r in records if r.service_name == service)
    groups = group_records(relevant, 5)
    assert {g.records[0].kind for g in groups} == {
        WorkKind.TICKET,
        WorkKind.RELEASE_CHECK,
        WorkKind.RUNBOOK,
    }
    assert all(len(g.records) == 5 for g in groups)
    receipts = await for_service(database, await scan(database), service)
    assert len(receipts) == 3
    async with database.session() as session:
        methods = set()
        for receipt in receipts:
            evidence = await LedgerService(session).evidence_for_task(UUID(receipt.task_id))
            suggestion = Suggestion.model_validate_json(json.dumps(evidence[0].result_snapshot))
            methods.add(suggestion.method)
        assert methods == {"workflow", "self_healing"}


async def test_five_closed_fake_incidents_use_postmortem_root_cause_and_evidence(
    database: Database,
) -> None:
    began = utc_now()
    settings = Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})
    requests = [await closed_fake_incident(database, settings) for _ in range(5)]
    async with database.session() as session:
        records = await collect_records(session, ScanWindow(start=began, end=utc_now()))
    incidents = tuple(r for r in records if r.kind is WorkKind.INCIDENT)
    assert len(incidents) == 5
    assert {r.task_id for r in incidents} == {r.task_id for r in requests}
    assert len(group_records(incidents, 5)) == 1
    assert all(r.evidence_id == r.record_id for r in incidents)
    result = await scan(database)
    async with database.session() as session:
        suggestions = [
            await LedgerService(session).get_evidence(UUID(i)) for i in result.evidence_ids
        ]
        assert any(
            isinstance(e.result_snapshot, dict)
            and e.result_snapshot.get("method") == "automatic_runbook"
            for e in suggestions
        )


class LoseScanResponse:
    def __init__(self, delegate: AutomationActivities) -> None:
        self.delegate = delegate
        self.calls = 0

    @activity.defn(name="automation.scan")
    async def scan(self, request: ScanRequest) -> AutomationResult:
        result = await self.delegate.scan(request)
        self.calls += 1
        if self.calls == 1:
            raise ApplicationError("模拟提交后丢响应")
        return result


@local_temporal
async def test_temporal_commit_retry_worker_restart_dispatch_and_history_replay(
    database: Database,
) -> None:
    service = f"automation-{uuid4().hex}"
    await seed_manual(database, service, 5)
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    queue = f"automation-{uuid4().hex}"
    settings = Settings(APP_ENV="test", TEMPORAL_CONFIG={"task_queue": queue})
    fail_once = LoseScanResponse(AutomationActivities(database, settings))
    event_activities = EventActivities(database, settings, client)
    result = None
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[AutomationDiscoveryWorkflow],
            activities=[fail_once.scan, event_activities.start_task],
        ):
            handle = await client.start_workflow(
                AutomationDiscoveryWorkflow.run,
                AutomationInput(),
                id=f"automation-{uuid4().hex}",
                task_queue=queue,
            )
            result = await asyncio.wait_for(handle.result(), 60)
            assert fail_once.calls == 2
        receipts = await for_service(database, result, service)
        assert len(receipts) == 1 and receipts[0].duplicate
        # 任务已派发但未运行；启动同一入口的新 Worker 恢复生命周期。
        async with create_worker(client, database, settings):
            task_handle = client.get_workflow_handle_for(
                AITaskWorkflow.run, receipts[0].workflow_id
            )
            progress = await wait_at(task_handle, TaskStatus.WAITING_INFORMATION)
            assert progress.task_id == receipts[0].task_id
            await event_activities.start_task(receipts[0])
            again = await client.execute_workflow(
                AutomationDiscoveryWorkflow.run,
                AutomationInput(),
                id=f"automation-{uuid4().hex}",
                task_queue=queue,
            )
            assert (await for_service(database, again, service))[0].task_id == receipts[0].task_id
        await Replayer(workflows=[AutomationDiscoveryWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
    finally:
        if result:
            for receipt in result.receipts:
                await client.get_workflow_handle(receipt.workflow_id).terminate("隔离验收结束")


@local_temporal
async def test_actual_schedule_triggers_and_registration_preserves_pause(
    database: Database,
) -> None:
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    queue = f"automation-schedule-{uuid4().hex}"
    config = AutomationConfig(interval_seconds=2, schedule_id=f"automation-test-{uuid4().hex}")
    settings = Settings(
        APP_ENV="test", AUTOMATION_CONFIG=config, TEMPORAL_CONFIG={"task_queue": queue}
    )

    async def ignore_suggestion(receipt: EventReceipt) -> None:
        pass

    start = activity.defn(name="event.start_task")(ignore_suggestion)
    running_ids: set[str] = set()
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[AutomationDiscoveryWorkflow],
            activities=[AutomationActivities(database, settings).scan, start],
        ):
            assert await ensure_automation_schedule(client, config, queue)
            schedule = client.get_schedule_handle(config.schedule_id)
            async with asyncio.timeout(30):
                while True:
                    description = await schedule.describe()
                    if len(description.info.recent_actions) >= 2:
                        break
                    await asyncio.sleep(0.1)
            await schedule.pause(note="隔离验收暂停")
            assert not await ensure_automation_schedule(client, config, "other-queue")
            assert (await schedule.describe()).schedule.state.paused
            for entry in description.info.recent_actions:
                execution = entry.action
                assert isinstance(execution, ScheduleActionExecutionStartWorkflow)
                running_ids.add(execution.workflow_id)
                await asyncio.wait_for(
                    client.get_workflow_handle(execution.workflow_id).result(), 15
                )
    finally:
        await client.get_schedule_handle(config.schedule_id).delete()
