"""仅本机临时 PostgreSQL/Temporal，运维系统及凭证全为 Fake。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

from app.config import Settings, parse_database_url
from app.connectors.war_room.facts import FakeWarRoomConnector, WarRoomFacts, WarRoomQuery
from app.db.base import utc_now
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.activities import TaskActivityStore
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor, VerificationRequired
from app.tasks.war_room.activities import WarRoomActivities
from app.tasks.war_room.demo import drive, stop_anomalies
from app.tasks.war_room.models import (
    SECTIONS,
    WarRoomAssessment,
    WarRoomRequest,
    WarRoomResult,
    WarRoomSubmission,
    WarRoomVerifyRequest,
)
from app.tasks.war_room.scenario import fake_resources, seed_runbook
from app.tasks.war_room.service import require_war_room_plan, submit_war_room
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.war_room import war_room_registry
from app.triggers.activities import EventActivities
from app.triggers.schemas import EventReceipt
from tests.database_support import get_test_database_url, migrate

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"), reason="运行 check-war-room.ps1 使用本机临时依赖"
    ),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    database = Database(parse_database_url(get_test_database_url()))
    try:
        yield database
    finally:
        await database.dispose()


def options(service: str) -> Settings:
    _, binding = fake_resources(service)
    return Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True, "bindings": (binding,)},
        TEMPORAL_CONFIG={
            "address": os.environ.get("TEST_TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            "task_queue": "war-room-test-" + uuid4().hex,
            "human_timeout_seconds": 2.0,
        },
        WAR_ROOM_CONFIG={"interval_seconds": 1.0, "max_windows": 100},
    )


def value(service: str, *, live: bool = False, rps: float = 1000.0) -> WarRoomSubmission:
    now = utc_now()
    return WarRoomSubmission(
        request_id=uuid4(),
        service_name=service,
        title="高峰保障",
        kind="peak",
        start=now + timedelta(seconds=3),
        end=now + timedelta(seconds=10 if live else 50),
        projected_rps=rps,
    )


async def submit(database: Database, submission: WarRoomSubmission) -> EventReceipt:
    async with database.session() as session, session.begin():
        return await submit_war_room(session, submission)


async def prepare(database: Database, service: str) -> WarRoomRequest:
    receipt = await submit(database, value(service))
    snapshot = TaskSnapshot(receipt.task_id, TaskStatus.NEW, 0)
    for status in (
        TaskStatus.CONTEXT_BUILDING,
        TaskStatus.RUNBOOK_MATCHING,
        TaskStatus.INVESTIGATING,
        TaskStatus.RCA,
    ):
        snapshot = await TaskActivityStore(database).transition(
            TransitionRequest(snapshot, status, "保障专项准备")
        )
    return WarRoomRequest(snapshot)


async def test_submission_concurrent_dedup_overlap_and_immutable_input(database: Database) -> None:
    service = "war-" + uuid4().hex[:12]
    submitted = value(service)
    first, second = await asyncio.gather(submit(database, submitted), submit(database, submitted))
    assert first.task_id == second.task_id and first.duplicate != second.duplicate
    with pytest.raises(ValueError):
        await submit(database, submitted.model_copy(update={"projected_rps": 2000.0}))
    with pytest.raises(ValueError):
        await submit(database, submitted.model_copy(update={"request_id": uuid4()}))


async def test_assessment_checkpoint_retries_query_only_once(database: Database) -> None:
    service = "war-" + uuid4().hex[:12]
    await seed_runbook(database, service)
    request = await prepare(database, service)
    resources, _ = fake_resources(service)
    facts = FakeWarRoomConnector()
    activities = WarRoomActivities(database, options(service), facts=facts, resources=resources)
    first, second = await asyncio.gather(activities.assess(request), activities.assess(request))
    assert first == second == await activities.assess(request) and facts.calls == 1
    assessed = WarRoomAssessment.model_validate_json(first.report_json)
    assert assessed.complete and assessed.safe and assessed.required_replicas == 5
    async with database.session() as session:
        calls = [
            a
            for a in await LedgerService(session).audits_for_task(UUID(request.task.task_id))
            if a.event_type is AuditEventType.TOOL_CALL
        ]
        assert [c.operation for c in calls] == [
            "search_runbooks",
            "query_war_room_facts",
            "get_execution_target",
        ]
    with pytest.raises(ApplicationError):
        await activities.assess(replace(request, task=replace(request.task, version=100)))


async def test_policy_denial_retains_audit_and_prevents_queries(database: Database) -> None:
    service = "war-" + uuid4().hex[:12]
    await seed_runbook(database, service)
    request = await prepare(database, service)
    settings = options(service).model_copy(
        update={
            "policy_config": Settings(
                APP_ENV="test",
                POLICY_CONFIG={
                    "rules": [
                        {
                            "id": "deny-read",
                            "action_names": ["query_war_room_facts"],
                            "risk_levels": ["L0"],
                            "reason": "专项",
                            "decision": "deny",
                        }
                    ]
                },
            ).policy_config
        }
    )
    resources, _ = fake_resources(service)
    facts = FakeWarRoomConnector()
    result = await WarRoomActivities(database, settings, resources=resources, facts=facts).assess(
        request
    )
    assert result.blocked and facts.calls == 0
    async with database.session() as session:
        assert any(
            a.operation == "query_war_room_facts" and a.outcome == "rejected"
            for a in await LedgerService(session).audits_for_task(UUID(request.task.task_id))
        )


async def test_missing_runbook_is_unknown_and_reviewer_cannot_plan(database: Database) -> None:
    service = "war-" + uuid4().hex[:12]
    request = await prepare(database, service)
    resources, _ = fake_resources(service)
    activities = WarRoomActivities(database, options(service), resources=resources)
    result = await activities.assess(request)
    assert not WarRoomAssessment.model_validate_json(result.report_json).complete
    await activities.review(WarRoomVerifyRequest(request.task, result.evidence_id))
    with pytest.raises(ValueError):
        await TaskActivityStore(database).transition(
            TransitionRequest(request.task, TaskStatus.PLANNING, "无已审核 Runbook")
        )


async def test_replay_reads_original_snapshot_without_source_calls(database: Database) -> None:
    service = "war-" + uuid4().hex[:12]
    await seed_runbook(database, service)
    request = await prepare(database, service)
    resources, _ = fake_resources(service)
    facts = FakeWarRoomConnector()
    settings = options(service)
    result = await WarRoomActivities(database, settings, resources=resources, facts=facts).assess(
        request
    )
    assessment = WarRoomAssessment.model_validate_json(result.report_json)
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        original = await ledger.get_evidence(assessment.facts_evidence_id)
        replay = await ToolDispatcher(
            war_room_registry(session, settings, facts, resources),
            create_policy_engine(settings),
            ledger,
        ).dispatch(
            task_id=UUID(request.task.task_id),
            tool_name="query_war_room_facts",
            parameters=original.parameters,
            actor="replay",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=original.id,
            replay_before=utc_now(),
        )
        assert (
            replay.status is DispatchStatus.REPLAYED and replay.result == original.result_snapshot
        )
    assert facts.calls == 1


@local_temporal
@pytest.mark.parametrize(
    "case, count, status",
    [
        ("normal", 2, TaskStatus.CLOSED),
        ("anomaly", 2, TaskStatus.CLOSED),
        ("rejected", 0, TaskStatus.ESCALATED),
        ("timeout", 0, TaskStatus.ESCALATED),
        ("ownership", 1, TaskStatus.ESCALATED),
        ("high_load", 1, TaskStatus.ESCALATED),
        ("verification_failure", 2, TaskStatus.ESCALATED),
    ],
)
async def test_complete_workflow_and_unsafe_branches(
    database: Database, case: str, count: int, status: TaskStatus
) -> None:
    service = "war-" + uuid4().hex[:12]
    resources, _ = fake_resources(service)
    settings = options(service)
    await seed_runbook(database, service)

    class InjectedFacts(FakeWarRoomConnector):
        async def query(self, query: WarRoomQuery) -> WarRoomFacts:
            snapshot = await super().query(query)
            if query.purpose == "cleanup" and case == "ownership":
                resources.targets[service] = resources.targets[service].model_copy(
                    update={"resource_version": "99"}
                )
            if query.purpose == "cleanup" and case == "high_load":
                return snapshot.model_copy(update={"current_rps": 2000.0})
            if query.purpose == "verify" and case == "verification_failure":
                return snapshot.model_copy(update={"rollback_ready": False})
            return snapshot

    facts = InjectedFacts(abnormal_windows=frozenset({0, 1}) if case == "anomaly" else frozenset())
    activities = WarRoomActivities(database, settings, resources=resources, facts=facts)
    receipt = await submit(database, value(service, live=True))
    client = await Client.connect(settings.temporal_config.address)
    handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
    try:
        async with create_worker(
            client,
            database,
            settings,
            war_room_activities=activities,
            executor_activities=ExecutorActivities(database, settings, connector=resources),
        ):
            events = EventActivities(database, settings, client)
            await events.start_task(receipt)
            await events.start_task(receipt)
            result = (
                await asyncio.wait_for(handle.result(), 30)
                if case == "timeout"
                else await drive(handle, decision="rejected" if case == "rejected" else "approved")
            )
        assert result.task and result.task.status is status and resources.execution_count == count
        if count == 0:
            assert resources.issue_count == 0
        async with database.session() as session:
            ledger = LedgerService(session)
            history = await TaskService(session).history(UUID(receipt.task_id))
            assert [h.to_status for h in history] == [s.status for s in result.history]
            if status == TaskStatus.CLOSED:
                report = await ledger.get_evidence(UUID(result.postmortem_evidence_id or ""))
                data = json.loads(json.dumps(report.result_snapshot))
                assert tuple(s["name"] for s in data["sections"]) == SECTIONS
                assert len(data["anomaly_tasks"]) == (1 if case == "anomaly" else 0)
                assert len(data["approvals"]) == 2 and resources.targets[service].replicas == 3
                if case == "anomaly":
                    child = client.get_workflow_handle("ai-task-" + data["anomaly_tasks"][0])
                    assert (await child.describe()).workflow_type == "AITaskWorkflow"
                    child_task = await session.get(AITask, UUID(data["anomaly_tasks"][0]))
                    assert child_task is not None
                    assert child_task.status in {
                        TaskStatus.WAITING_INFORMATION,
                        TaskStatus.ESCALATED,
                    }
            else:
                assert not any(h.to_status == TaskStatus.RESOLVED for h in history)
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate("清理重大保障专项主任务")
        await stop_anomalies(client, database, UUID(receipt.task_id))


@local_temporal
async def test_committed_assessment_worker_restart_does_not_repeat_read(database: Database) -> None:
    service = "war-" + uuid4().hex[:12]
    resources, _ = fake_resources(service)
    settings = options(service)
    await seed_runbook(database, service)
    committed = asyncio.Event()

    class LostReply(WarRoomActivities):
        attempts = 0

        @activity.defn(name="war_room.assess")
        async def assess(self, request: WarRoomRequest) -> WarRoomResult:
            result = await super().assess(request)
            if request.purpose == "prepare":
                self.attempts += 1
                if self.attempts == 1:
                    committed.set()
                    raise ApplicationError("模拟检查提交后丢响应")
            return result

    facts = FakeWarRoomConnector()
    activities = LostReply(database, settings, resources=resources, facts=facts)
    receipt = await submit(database, value(service, live=True))
    client = await Client.connect(settings.temporal_config.address)
    handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
    executor = ExecutorActivities(database, settings, connector=resources)
    try:
        async with create_worker(
            client, database, settings, war_room_activities=activities, executor_activities=executor
        ):
            await EventActivities(database, settings, client).start_task(receipt)
            await asyncio.wait_for(committed.wait(), 20)
        async with create_worker(
            client, database, settings, war_room_activities=activities, executor_activities=executor
        ):
            result = await drive(handle)
        assert (
            result.task
            and result.task.status is TaskStatus.CLOSED
            and resources.execution_count == 2
        )
        assert activities.attempts >= 2
        async with database.session() as session:
            audits = await LedgerService(session).audits_for_task(UUID(receipt.task_id))
            assert (
                sum(a.operation == "query_war_room_facts" and a.actor == "war-room" for a in audits)
                == 9
            )
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate("清理保障 Worker 重启测试")
        await stop_anomalies(client, database, UUID(receipt.task_id))


async def test_external_verifier_identity_cannot_resolve(database: Database) -> None:
    service = "war-" + uuid4().hex[:12]
    await seed_runbook(database, service)
    request = await prepare(database, service)
    resources, _ = fake_resources(service)
    activities = WarRoomActivities(database, options(service), resources=resources)
    assessment = await activities.assess(request)
    await activities.review(WarRoomVerifyRequest(request.task, assessment.evidence_id))
    snapshot = request.task
    for status in (TaskStatus.PLANNING, TaskStatus.EXECUTING, TaskStatus.VERIFYING):
        snapshot = await TaskActivityStore(database).transition(
            TransitionRequest(snapshot, status, "平台内部测试阶段，无写操作")
        )
    async with database.session() as session, session.begin():
        task = await session.get(AITask, UUID(request.task.task_id))
        assert task is not None
        with pytest.raises(VerificationRequired):
            # 无内部权限，即便调用方使用 verifier 身份也不能设置成功。
            await TaskService(session).transition(
                task.id,
                TaskStatus.RESOLVED,
                expected_status=TaskStatus.VERIFYING,
                expected_version=snapshot.version,
                reason="伪造身份",
                actor=TransitionActor.VERIFIER,
            )


@pytest.mark.parametrize("changed", ["parameters", "identity", "rules", "expired"])
async def test_plan_or_host_changes_and_expiry_invalidate_authorization(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    service = "war-" + uuid4().hex[:12]
    await seed_runbook(database, service)
    resources, _ = fake_resources(service)
    settings = options(service)
    activities = WarRoomActivities(database, settings, resources=resources)
    request = await prepare(database, service)
    assessed = await activities.assess(request)
    await activities.review(WarRoomVerifyRequest(request.task, assessed.evidence_id))
    snapshot = await TaskActivityStore(database).transition(
        TransitionRequest(request.task, TaskStatus.PLANNING, "准备精确保障计划")
    )
    generated = await activities.plan(WarRoomVerifyRequest(snapshot, assessed.evidence_id))
    plan = ActionPlan.model_validate_json(generated.plan_json)
    if changed == "parameters":
        item = plan.actions[0]
        plan = plan.model_copy(
            update={
                "actions": (
                    item.model_copy(
                        update={
                            "action": item.action.model_copy(
                                update={"parameters": {"from_replicas": 3, "to_replicas": 99}}
                            )
                        }
                    ),
                )
            }
        )
    elif changed == "identity":
        item = plan.actions[0]
        plan = plan.model_copy(
            update={
                "actions": (
                    item.model_copy(
                        update={"action": item.action.model_copy(update={"id": "unrelated-action"})}
                    ),
                )
            }
        )
    elif changed == "rules":
        settings = settings.model_copy(
            update={
                "war_room_config": settings.war_room_config.model_copy(
                    update={"capacity_margin": 2.0}
                )
            }
        )
    else:
        monkeypatch.setattr(
            "app.tasks.war_room.service.utc_now", lambda: utc_now() + timedelta(seconds=60)
        )
    async with database.session() as session:
        task = await session.get(AITask, UUID(snapshot.task_id))
        assert task is not None
        with pytest.raises((PermissionError, ValueError)):
            await require_war_room_plan(session, task, plan, settings)
    assert resources.issue_count == resources.execution_count == 0


async def test_reviewer_rejects_changed_capacity_assumption(database: Database) -> None:
    service = "war-" + uuid4().hex[:12]
    await seed_runbook(database, service)
    resources, _ = fake_resources(service)

    class CapacityChanged(FakeWarRoomConnector):
        async def query(self, query: WarRoomQuery) -> WarRoomFacts:
            result = await super().query(query)
            return (
                result if self.calls == 1 else result.model_copy(update={"per_replica_rps": 100.0})
            )

    activities = WarRoomActivities(
        database, options(service), resources=resources, facts=CapacityChanged()
    )
    request = await prepare(database, service)
    assessed = await activities.assess(request)
    await activities.review(WarRoomVerifyRequest(request.task, assessed.evidence_id))
    with pytest.raises(ValueError):
        await TaskActivityStore(database).transition(
            TransitionRequest(request.task, TaskStatus.PLANNING, "容量反证")
        )
    assert resources.issue_count == 0


@local_temporal
async def test_existing_capacity_requires_no_writes_and_successful_report(
    database: Database,
) -> None:
    service = "war-" + uuid4().hex[:12]
    await seed_runbook(database, service)
    resources, _ = fake_resources(service)
    settings = options(service)

    class LowDemand(FakeWarRoomConnector):
        async def query(self, query: WarRoomQuery) -> WarRoomFacts:
            return (await super().query(query)).model_copy(update={"current_rps": 400.0})

    receipt = await submit(database, value(service, live=True, rps=400.0))
    client = await Client.connect(settings.temporal_config.address)
    handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
    try:
        async with create_worker(
            client,
            database,
            settings,
            war_room_activities=WarRoomActivities(
                database, settings, resources=resources, facts=LowDemand()
            ),
            executor_activities=ExecutorActivities(database, settings, connector=resources),
        ):
            await EventActivities(database, settings, client).start_task(receipt)
            result = await asyncio.wait_for(handle.result(), 30)
        assert result.task and result.task.status is TaskStatus.CLOSED
        assert resources.issue_count == resources.execution_count == 0
        async with database.session() as session:
            report = await LedgerService(session).get_evidence(
                UUID(result.postmortem_evidence_id or "")
            )
            data = json.loads(json.dumps(report.result_snapshot))
            assert len(data["sections"]) == 11 and data["executions"] == data["approvals"] == []
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate("清理无需写操作的保障测试")


@local_temporal
async def test_execute_lost_reply_retries_same_action_once(database: Database) -> None:
    from app.executor.models import ExecutionRequest, ExecutionResult

    service = "war-" + uuid4().hex[:12]
    await seed_runbook(database, service)
    resources, _ = fake_resources(service)
    settings = options(service)

    class LostReplyExecutor(ExecutorActivities):
        attempts = 0

        @activity.defn(name="executor.execute_action")
        async def execute(self, request: ExecutionRequest) -> ExecutionResult:
            result = await super().execute(request)
            self.attempts += 1
            if self.attempts == 1:
                raise ApplicationError("模拟资源准备提交后丢响应")
            return result

    receipt = await submit(database, value(service, live=True))
    client = await Client.connect(settings.temporal_config.address)
    handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
    executor = LostReplyExecutor(database, settings, connector=resources)
    try:
        async with create_worker(
            client,
            database,
            settings,
            executor_activities=executor,
            war_room_activities=WarRoomActivities(database, settings, resources=resources),
        ):
            await EventActivities(database, settings, client).start_task(receipt)
            result = await drive(handle)
        assert result.task and result.task.status is TaskStatus.CLOSED
        assert resources.execution_count == resources.issue_count == 2 and executor.attempts == 3
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate("清理保障执行丢响应测试")
