"""隔离本机 PostgreSQL/Temporal 的发布闭环与授权验收。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import parse_database_url
from app.connectors.changes.releases import FakeReleaseState
from app.db.session import Database
from app.executor.models import ExecutionRequest
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.releases.activities import ReleaseActivities
from app.tasks.releases.demo import approve_phase, demo_settings, push_release, wait_phase
from app.tasks.releases.models import ReleaseAssessment
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus, TransitionActor
from app.tasks.tickets.demo import wait_progress
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TaskSnapshot, WorkflowProgress
from app.triggers.activities import EventActivities
from tests.database_support import get_test_database_url, migrate

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("TEST_TEMPORAL_ADDRESS"),
        reason="执行 check-releases.ps1",
    ),
]


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        yield instance
    finally:
        await instance.dispose()


async def client() -> Client:
    return await Client.connect(
        os.environ["TEST_TEMPORAL_ADDRESS"],
        namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default"),
    )


@pytest.mark.parametrize(
    "scenario,outcome,writes", [("normal", "released", 2), ("anomaly", "rolled_back", 3)]
)
async def test_release_closed_report_evidence_and_replay(
    database: Database, scenario: str, outcome: str, writes: int
) -> None:
    temporal = await client()
    settings = demo_settings("release-test-" + uuid4().hex)
    state = FakeReleaseState(scenario=scenario)
    activities = ReleaseActivities(database, settings, state=state)
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    try:
        async with create_worker(temporal, database, settings, release_activities=activities):
            events = EventActivities(database, settings, temporal)
            await events.start_task(receipt)
            await events.start_task(receipt)
            first = await wait_phase(handle, "canary")
            assert state.writer.issue_count == 0
            await approve_phase(handle, "canary")
            purpose = "promote" if scenario == "normal" else "rollback"
            second = await wait_phase(handle, purpose)
            if scenario == "anomaly":
                assert state.writer.targets["payment-service"].paused
                assert state.writer.execution_count == 2
            assert first.approval_prompt and second.approval_prompt
            assert first.approval_prompt.action_hash != second.approval_prompt.action_hash
            await approve_phase(handle, purpose)
            result = await asyncio.wait_for(handle.result(), 60)
            assert result.task and result.task.status is TaskStatus.CLOSED
            assert state.writer.execution_count == writes
            async with database.session() as session:
                task = await session.get(AITask, UUID(receipt.task_id))
                assert task and task.source is TaskSource.RELEASE
                records = await LedgerService(session).evidence_for_task(task.id)
                report = next(e for e in records if e.source_tool == "release.report")
                assert isinstance(report.result_snapshot, dict)
                assert report.result_snapshot["outcome"] == outcome
                timeline = report.result_snapshot["timeline"]
                assert isinstance(timeline, list)
                assert (
                    sum(
                        isinstance(item, dict) and item.get("source") == "approval.decision"
                        for item in timeline
                    )
                    == 2
                )
                references = report.result_snapshot["evidence_ids"]
                assert isinstance(references, list)
                assert all(
                    isinstance(i, str) and UUID(i) in {e.id for e in records} for i in references
                )
                checks = ReleaseAssessment.model_validate_json(
                    json.dumps(report.result_snapshot["checks"])
                )
                assert checks.passed and {c.name for c in checks.checks} == {
                    "git_diff",
                    "impact",
                    "sql",
                    "resources",
                    "monitoring",
                    "rollback",
                }
                histories = await TaskService(session).history(task.id)
                assert [(h.to_status, h.sequence) for h in histories] == [
                    (h.status, h.version) for h in result.history
                ]
                audits = await LedgerService(session).audits_for_task(task.id)
                assert (
                    sum(
                        a.event_type is AuditEventType.EXECUTION and a.outcome == "succeeded"
                        for a in audits
                    )
                    == writes
                )
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
            assert first.action_plan_json and first.action_plan_evidence_id
            plan = ActionPlan.model_validate_json(first.action_plan_json)
            request = ExecutionRequest(
                TaskSnapshot(receipt.task_id, TaskStatus.EXECUTING, plan.planning_version + 2),
                first.action_plan_evidence_id,
                first.approval_prompt,
            )
            replayed = await asyncio.gather(
                *(activities.executor.execute(request) for _ in range(3))
            )
            assert (
                len({tuple(r.evidence_ids) for r in replayed}) == 1
                and state.writer.execution_count == writes
            )
            before = state.writer.issue_count
            await activities.executor.replay(request, UUID(replayed[0].evidence_ids[0]))
            assert state.writer.issue_count == before and state.writer.execution_count == writes
    finally:
        if (await handle.describe()).close_time is None:
            await handle.terminate("发布测试清理")


@pytest.mark.parametrize("decision", ["rejected", "timeout", "changed"])
async def test_initial_release_rejected_expired_or_changed_never_writes(
    database: Database, decision: str
) -> None:
    temporal = await client()
    settings = demo_settings(
        "release-rejected-" + uuid4().hex, timeout=1 if decision == "timeout" else 30
    )
    state = FakeReleaseState()
    activities = ReleaseActivities(database, settings, state=state)
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    async with create_worker(temporal, database, settings, release_activities=activities):
        await EventActivities(database, settings, temporal).start_task(receipt)
        if decision == "changed":
            await wait_phase(handle, "canary")
            release_id = next(iter(state.requests))
            state.requests[release_id] = state.requests[release_id].model_copy(
                update={"sql": ("DROP TABLE payments",)}
            )
            await approve_phase(handle, "canary")
        elif decision == "rejected":
            await approve_phase(handle, "canary", decision=decision)
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is TaskStatus.ESCALATED
        assert state.writer.issue_count == state.writer.execution_count == 0


async def test_high_risk_sql_is_blocked_before_approval(database: Database) -> None:
    temporal = await client()
    settings = demo_settings("release-sql-" + uuid4().hex)
    state = FakeReleaseState(scenario="high_sql")
    activities = ReleaseActivities(database, settings, state=state)
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    try:
        async with create_worker(temporal, database, settings, release_activities=activities):
            await EventActivities(database, settings, temporal).start_task(receipt)
            progress = await wait_progress(handle, TaskStatus.NEED_HUMAN_JUDGMENT)
            assert progress.conclusion_json and not progress.approval_prompt
            assessment = ReleaseAssessment.model_validate_json(progress.conclusion_json)
            check = next(c for c in assessment.checks if c.name == "sql")
            assert not check.passed and "L4" in check.claim.statement and check.claim.evidence_ids
            assert state.writer.issue_count == state.writer.execution_count == 0
            async with database.session() as session, session.begin():
                task = await session.get(AITask, UUID(receipt.task_id))
                assert task
                with pytest.raises(ValueError):
                    await TaskService(session).transition(
                        task.id,
                        TaskStatus.PLANNING,
                        expected_status=task.status,
                        expected_version=task.status_version,
                        reason="伪造绕过",
                    )
    finally:
        await handle.terminate("发布 SQL 测试清理")


async def test_no_approval_and_false_verifier_identity_rejected(database: Database) -> None:
    temporal = await client()
    settings = demo_settings("release-authority-" + uuid4().hex)
    state = FakeReleaseState()
    activities = ReleaseActivities(database, settings, state=state)
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    try:
        async with create_worker(temporal, database, settings, release_activities=activities):
            await EventActivities(database, settings, temporal).start_task(receipt)
            progress = await wait_phase(handle, "canary")
            assert progress.action_plan_evidence_id and progress.task
            with pytest.raises((ValueError, PermissionError)):
                await activities.executor.execute(
                    ExecutionRequest(progress.task, progress.action_plan_evidence_id)
                )
            async with database.session() as session, session.begin():
                task = await session.get(AITask, UUID(receipt.task_id))
                assert task
                with pytest.raises(ValueError):
                    await TaskService(session).transition(
                        task.id,
                        TaskStatus.RESOLVED,
                        expected_status=task.status,
                        expected_version=task.status_version,
                        reason="假冒验证",
                        actor=TransitionActor.VERIFIER,
                    )
            assert state.writer.issue_count == state.writer.execution_count == 0
    finally:
        await handle.terminate("发布权限测试清理")


async def test_continuing_deterioration_trips_safety_before_pause_or_rollback(
    database: Database,
) -> None:
    temporal = await client()
    settings = demo_settings("release-abort-" + uuid4().hex)
    state = FakeReleaseState(scenario="deteriorating")
    activities = ReleaseActivities(database, settings, state=state)
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    async with create_worker(temporal, database, settings, release_activities=activities):
        await EventActivities(database, settings, temporal).start_task(receipt)
        await approve_phase(handle, "canary")
        result = await asyncio.wait_for(handle.result(), 60)
        assert (
            result.task
            and result.task.status is TaskStatus.AUTOMATION_ABORTED
            and result.takeover_notification_state == "sent"
        )
        assert state.writer.execution_count == 1


async def test_worker_restart_keeps_rollback_approval_separate(database: Database) -> None:
    temporal = await client()
    settings = demo_settings("release-restart-" + uuid4().hex)
    state = FakeReleaseState(scenario="anomaly")
    activities = ReleaseActivities(database, settings, state=state)
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    async with create_worker(temporal, database, settings, release_activities=activities):
        await EventActivities(database, settings, temporal).start_task(receipt)
        await approve_phase(handle, "canary")
        waiting = await wait_phase(handle, "rollback")
    async with create_worker(
        temporal,
        database,
        settings,
        release_activities=ReleaseActivities(database, settings, state=state),
    ):
        restored = await wait_phase(handle, "rollback")
        assert restored.approval_prompt == waiting.approval_prompt
        await approve_phase(handle, "rollback", decision="rejected")
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is TaskStatus.ESCALATED
        assert state.writer.execution_count == 2 and state.writer.targets["payment-service"].paused


@pytest.mark.parametrize("scenario", ["missing", "unrecovered"])
async def test_missing_or_unrecovered_facts_never_resolve(
    database: Database, scenario: str
) -> None:
    temporal = await client()
    settings = demo_settings("release-unresolved-" + uuid4().hex)
    state = FakeReleaseState(scenario=scenario)
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    async with create_worker(
        temporal,
        database,
        settings,
        release_activities=ReleaseActivities(database, settings, state=state),
    ):
        await EventActivities(database, settings, temporal).start_task(receipt)
        await approve_phase(handle, "canary")
        await approve_phase(handle, "rollback")
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is TaskStatus.ESCALATED
        assert not any(t.status is TaskStatus.RESOLVED for t in result.history)
        assert result.postmortem_evidence_id is None and state.writer.execution_count == 3


async def test_default_pause_also_requires_distinct_approval(database: Database) -> None:
    temporal = await client()
    settings = demo_settings("release-default-pause-" + uuid4().hex, auto_pause=False)
    state = FakeReleaseState(scenario="anomaly")
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    async with create_worker(
        temporal,
        database,
        settings,
        release_activities=ReleaseActivities(database, settings, state=state),
    ):
        await EventActivities(database, settings, temporal).start_task(receipt)
        await approve_phase(handle, "canary")
        waiting = await wait_phase(handle, "pause")
        assert waiting.action_plan_json and "need_approval" in waiting.action_plan_json
        assert (
            not state.writer.targets["payment-service"].paused and state.writer.execution_count == 1
        )
        await approve_phase(handle, "pause", decision="rejected")
        result = await asyncio.wait_for(handle.result(), 60)
        assert (
            result.task
            and result.task.status is TaskStatus.ESCALATED
            and state.writer.execution_count == 1
        )


async def test_policy_deny_release_has_no_execution_or_approval(database: Database) -> None:
    from app.policy.models import PolicyConfig

    temporal = await client()
    settings = demo_settings("release-deny-" + uuid4().hex)
    settings = settings.model_copy(
        update={
            "policy_config": PolicyConfig.model_validate(
                {
                    "rules": [
                        {
                            "id": "deny-release",
                            "risk_levels": ["L3"],
                            "action_names": ["deploy_service"],
                            "decision": "deny",
                            "reason": "禁止发布",
                        }
                    ]
                }
            )
        }
    )
    state = FakeReleaseState()
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    async with create_worker(
        temporal,
        database,
        settings,
        release_activities=ReleaseActivities(database, settings, state=state),
    ):
        await EventActivities(database, settings, temporal).start_task(receipt)
        result = await asyncio.wait_for(handle.result(), 60)
        assert (
            result.task
            and result.task.status is TaskStatus.ESCALATED
            and result.approval_prompt is None
        )
        assert state.writer.issue_count == state.writer.execution_count == 0


async def test_changed_host_binding_after_approval_cannot_issue(database: Database) -> None:
    from app.connectors.kubernetes.execution import fake_binding
    from app.executor.models import ExecutionConfig

    temporal = await client()
    settings = demo_settings("release-binding-" + uuid4().hex)
    state = FakeReleaseState()
    activities = ReleaseActivities(database, settings, state=state)
    receipt = await push_release(database, settings, state)
    handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
    async with create_worker(temporal, database, settings, release_activities=activities):
        await EventActivities(database, settings, temporal).start_task(receipt)
        await wait_phase(handle, "canary")
        binding = fake_binding().model_copy(update={"deployment_name": "other-deployment"})
        activities.executor.settings = settings.model_copy(
            update={"execution_config": ExecutionConfig(enabled=True, bindings=(binding,))}
        )
        await approve_phase(handle, "canary")
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is TaskStatus.ESCALATED
        assert state.writer.issue_count == state.writer.execution_count == 0
