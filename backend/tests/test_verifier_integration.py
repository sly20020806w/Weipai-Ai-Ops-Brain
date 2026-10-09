"""本机 PostgreSQL/Temporal：独立验证、伪造拒绝、原子性、重试与 Replay。"""

import asyncio
import json
import os
from dataclasses import replace
from functools import partial
from uuid import UUID, uuid4

import pytest
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.activities import TaskActivityStore
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus, TransitionActor, VerificationRequired
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest, WorkflowInput
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.verification_runtime import fake_verification_registry
from app.verifier.activities import VerifierActivities
from app.verifier.models import VerificationReport, VerificationRequest, VerificationResult
from app.verifier.scenario import sample_spec
from tests.test_tasks_integration import database, migrated_schema, path_to

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-verifier.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


async def verifying_task(database: Database) -> TaskSnapshot:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="独立验证样例", reason="Fake 验收前置任务"
        )
        snapshot = TaskSnapshot(str(task.id), task.status, task.status_version)
    store = TaskActivityStore(database)
    for target in path_to(TaskStatus.VERIFYING):
        snapshot = await store.transition(
            TransitionRequest(snapshot, target, "无运维副作用的验收前置阶段")
        )
    return snapshot


async def verify(
    database: Database,
    snapshot: TaskSnapshot,
    *,
    recovered: bool = True,
    settings: Settings | None = None,
) -> VerificationResult:
    return await VerifierActivities(
        database,
        settings or Settings(APP_ENV="test"),
        registry_factory=partial(fake_verification_registry, recovered=recovered),
    ).verify(VerificationRequest(snapshot, sample_spec(snapshot).model_dump_json()))


@pytest.mark.parametrize("recovered", [True, False])
async def test_recovered_or_failed_task_has_independent_evidence(
    database: Database, recovered: bool
) -> None:
    snapshot = await verifying_task(database)
    result = await verify(database, snapshot, recovered=recovered)
    report = VerificationReport.model_validate_json(result.report_json)
    assert report.passed is recovered
    stored = json.loads(result.report_json)
    assert "config" not in stored and "max_5xx_ratio" not in result.report_json
    assert len(report.criteria_hash) == 64
    assert result.task.status is (TaskStatus.RESOLVED if recovered else TaskStatus.INVESTIGATING)
    async with database.session() as session:
        ledger = LedgerService(session)
        evidence = await ledger.evidence_for_task(UUID(snapshot.task_id))
        assert len(evidence) == 9 and len({e.id for e in evidence}) == 9
        aggregate = await ledger.get_evidence(UUID(result.evidence_id))
        assert aggregate.source_tool == "verify_action"
        assert (
            VerificationReport.model_validate_json(json.dumps(aggregate.result_snapshot)) == report
        )
        assert {check.evidence_id for check in report.checks} == {
            e.id for e in evidence if e.id != aggregate.id
        }
        audits = await ledger.audits_for_task(UUID(snapshot.task_id))
        tool_audits = [a for a in audits if a.event_type is AuditEventType.TOOL_CALL]
        assert len(tool_audits) == 9 and all(
            a.actor == "verifier" and a.outcome == "succeeded" for a in tool_audits
        )
        history = await TaskService(session).history(UUID(snapshot.task_id))
        assert (
            history[-1].actor is TransitionActor.VERIFIER
            and result.evidence_id in history[-1].reason
        )


async def test_concurrent_and_lost_response_retry_never_resamples(database: Database) -> None:
    snapshot = await verifying_task(database)
    results = await asyncio.gather(verify(database, snapshot), verify(database, snapshot))
    assert results[0] == results[1]
    # 重试使用提交报告；注入另一组故障事实也不能改写已提交结果。
    assert await verify(database, snapshot, recovered=False) == results[0]
    later = await TaskActivityStore(database).transition(
        TransitionRequest(results[0].task, TaskStatus.LEARNING, "测试更晚阶段")
    )
    assert later.status is TaskStatus.LEARNING
    assert await verify(database, snapshot) == results[0]
    async with database.session() as session:
        assert len(await LedgerService(session).evidence_for_task(UUID(snapshot.task_id))) == 9


@pytest.mark.parametrize("tamper", ["task", "version", "target", "config"])
async def test_stale_or_changed_verification_cannot_reuse_success(
    database: Database, tamper: str
) -> None:
    snapshot = await verifying_task(database)
    await verify(database, snapshot)
    spec = sample_spec(snapshot)
    if tamper == "task":
        spec = spec.model_copy(update={"task_id": uuid4()})
    elif tamper == "version":
        spec = spec.model_copy(update={"verifying_version": snapshot.version - 1})
    elif tamper == "target":
        spec = spec.model_copy(update={"expected_image": "other:v1"})
    settings = (
        Settings(APP_ENV="test", VERIFICATION_CONFIG={"max_p99_ms": 100.0})
        if tamper == "config"
        else Settings(APP_ENV="test")
    )
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError):
        await VerifierActivities(
            database, settings, registry_factory=fake_verification_registry
        ).verify(VerificationRequest(snapshot, spec.model_dump_json()))


@pytest.mark.parametrize("actor", [TransitionActor.WORKFLOW, TransitionActor.VERIFIER])
async def test_outside_verifier_cannot_set_resolved(
    database: Database, actor: TransitionActor
) -> None:
    snapshot = await verifying_task(database)
    async with database.session() as session, session.begin():
        with pytest.raises(VerificationRequired):
            await TaskService(session).transition(
                UUID(snapshot.task_id),
                TaskStatus.RESOLVED,
                expected_status=snapshot.status,
                expected_version=snapshot.version,
                reason="外部调用方伪造",
                actor=actor,
            )
    async with database.session() as session:
        task = await session.get(AITask, UUID(snapshot.task_id))
        assert task and task.status is TaskStatus.VERIFYING


async def test_policy_denied_fact_returns_to_investigation(database: Database) -> None:
    snapshot = await verifying_task(database)
    settings = Settings(
        APP_ENV="test",
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "block-metrics",
                    "action_names": ["query_metrics"],
                    "risk_levels": ["L0"],
                    "decision": "deny",
                    "reason": "验收拦截",
                }
            ]
        },
    )
    result = await verify(database, snapshot, settings=settings)
    report = VerificationReport.model_validate_json(result.report_json)
    assert result.task.status is TaskStatus.INVESTIGATING
    assert all(
        not c.passed and c.evidence_id is None for c in report.checks if c.name.startswith("http_")
    )
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(snapshot.task_id))
        assert sum(a.operation == "query_metrics" and a.outcome == "rejected" for a in audits) == 3


async def test_audit_failure_rolls_back_evidence_and_resolution(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = await verifying_task(database)
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> object:
        if kwargs.get("event_type") is AuditEventType.STATE_TRANSITION:
            raise RuntimeError("验证状态审计失败")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError, match="验证状态审计失败"):
        await verify(database, snapshot)
    async with database.session() as session:
        task = await session.get(AITask, UUID(snapshot.task_id))
        assert task and task.status is TaskStatus.VERIFYING
        assert await LedgerService(session).evidence_for_task(task.id) == []
    monkeypatch.setattr(LedgerService, "append_audit", original)
    assert (await verify(database, snapshot)).task.status is TaskStatus.RESOLVED


async def test_aggregate_replay_keeps_original_result_and_cannot_resolve(
    database: Database,
) -> None:
    snapshot = await verifying_task(database)
    result = await verify(database, snapshot)
    settings = Settings(APP_ENV="test")
    async with database.session() as session, session.begin():
        async with fake_verification_registry(settings, session, recovered=False) as registry:
            # Replay 不会执行引擎或任何 Connector；实现入口故意设为抛错。
            async def forbidden(value: object) -> object:
                raise AssertionError("Replay 执行了实现")

            registered = registry._get("verify_action")
            registry._tools["verify_action"] = replace(registered, invoke=forbidden)  # type: ignore[arg-type]
            replay = await ToolDispatcher(
                registry, create_policy_engine(settings), LedgerService(session)
            ).dispatch(
                task_id=UUID(snapshot.task_id),
                tool_name="verify_action",
                parameters=sample_spec(snapshot).model_dump(mode="json"),
                actor="replay",
                mode=DispatchMode.REPLAY,
                replay_evidence_id=UUID(result.evidence_id),
                replay_before=utc_now(),
            )
            assert replay.status is DispatchStatus.REPLAYED and replay.evidence_id == UUID(
                result.evidence_id
            )
            assert (
                VerificationReport.model_validate_json(json.dumps(replay.result)).model_dump_json()
                == result.report_json
            )
            assert len(await LedgerService(session).evidence_for_task(UUID(snapshot.task_id))) == 9


@local_temporal
@pytest.mark.parametrize("recovered", [True, False])
async def test_temporal_verification_and_history_replay(
    database: Database, recovered: bool
) -> None:
    snapshot = await verifying_task(database)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "namespace": os.environ.get("TEST_TEMPORAL_NAMESPACE", "default"),
            "task_queue": f"verifier-{uuid4().hex}",
        },
    )
    client = await Client.connect(
        settings.temporal_config.address, namespace=settings.temporal_config.namespace
    )
    activities = VerifierActivities(
        database,
        settings,
        registry_factory=partial(fake_verification_registry, recovered=recovered),
    )
    value = WorkflowInput(
        snapshot.task_id,
        verification_json=sample_spec(snapshot).model_dump_json(),
        postmortem_enabled=False,
    )
    async with create_worker(client, database, settings, verifier_activities=activities):
        handle = await start_task_workflow(
            client, value, task_queue=settings.temporal_config.task_queue
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.task and result.task.status is (
        TaskStatus.RESOLVED if recovered else TaskStatus.INVESTIGATING
    )
    assert result.verification_evidence_id and result.verification_json
    async with database.session() as session:
        history = await TaskService(session).history(UUID(snapshot.task_id))
        assert [(h.to_status, h.sequence) for h in history[-2:]] == [
            (h.status, h.version) for h in result.history
        ]
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


async def test_agent_tool_report_is_readonly_and_not_a_verifier_checkpoint(
    database: Database,
) -> None:
    snapshot = await verifying_task(database)
    settings = Settings(APP_ENV="test")
    async with database.session() as session, session.begin():
        async with fake_verification_registry(settings, session) as registry:
            result = await ToolDispatcher(
                registry, create_policy_engine(settings), LedgerService(session)
            ).dispatch(
                task_id=UUID(snapshot.task_id),
                tool_name="verify_action",
                parameters=sample_spec(snapshot).model_dump(mode="json"),
                actor="main_agent",
            )
            assert result.status is DispatchStatus.SUCCEEDED
            task = await session.get(AITask, UUID(snapshot.task_id))
            assert task and task.status is TaskStatus.VERIFYING
    verified = await verify(database, snapshot, recovered=False)
    assert verified.evidence_id != str(result.evidence_id)
    assert verified.task.status is TaskStatus.INVESTIGATING


@pytest.mark.parametrize("proof", ["failed", "other_task", "old_version"])
async def test_borrowed_or_failed_resolution_proof_rejected(database: Database, proof: str) -> None:
    from app.verifier.authority import _verification_scope

    target = await verifying_task(database)
    if proof == "failed":
        settings = Settings(APP_ENV="test")
        async with database.session() as session, session.begin():
            async with fake_verification_registry(settings, session, recovered=False) as registry:
                result = await ToolDispatcher(
                    registry, create_policy_engine(settings), LedgerService(session)
                ).dispatch(
                    task_id=UUID(target.task_id),
                    tool_name="verify_action",
                    parameters=sample_spec(target).model_dump(mode="json"),
                    actor="verifier",
                )
                assert result.evidence_id is not None
                proof_id = result.evidence_id
    else:
        source = await verifying_task(database)
        report = await verify(database, source)
        proof_id = UUID(report.evidence_id)
    async with database.session() as session, session.begin():
        # 模拟内部错误地携带其他报告，tasks 门禁仍必须拒绝。
        with _verification_scope(
            UUID(target.task_id),
            target.version - (proof == "old_version"),
            proof_id,
        ):
            with pytest.raises(VerificationRequired):
                await TaskService(session).transition(
                    UUID(target.task_id),
                    TaskStatus.RESOLVED,
                    expected_status=TaskStatus.VERIFYING,
                    expected_version=target.version,
                    reason="错误恢复证据",
                    actor=TransitionActor.VERIFIER,
                )


@local_temporal
async def test_temporal_lost_response_retries_committed_report(database: Database) -> None:
    from temporalio import activity
    from temporalio.exceptions import ApplicationError

    snapshot = await verifying_task(database)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "namespace": os.environ.get("TEST_TEMPORAL_NAMESPACE", "default"),
            "task_queue": f"verifier-retry-{uuid4().hex}",
        },
    )

    class LostResponseVerifier(VerifierActivities):
        attempts = 0

        @activity.defn(name="verifier.verify_action")
        async def verify(self, request: VerificationRequest) -> VerificationResult:
            result = await super().verify(request)
            self.attempts += 1
            if self.attempts == 1:
                raise ApplicationError("模拟提交后丢响应")
            return result

    activities = LostResponseVerifier(
        database, settings, registry_factory=fake_verification_registry
    )
    client = await Client.connect(
        settings.temporal_config.address, namespace=settings.temporal_config.namespace
    )
    async with create_worker(client, database, settings, verifier_activities=activities):
        handle = await start_task_workflow(
            client,
            WorkflowInput(
                snapshot.task_id,
                verification_json=sample_spec(snapshot).model_dump_json(),
                postmortem_enabled=False,
            ),
            task_queue=settings.temporal_config.task_queue,
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.task and result.task.status is TaskStatus.RESOLVED and activities.attempts == 2
    async with database.session() as session:
        assert len(await LedgerService(session).evidence_for_task(UUID(snapshot.task_id))) == 9
        history = await TaskService(session).history(UUID(snapshot.task_id))
        assert len(history) == snapshot.version + 2
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
