"""本机 PostgreSQL/Temporal：审核、独立验证计数、旧授权失效和重启回放。"""

import asyncio
import json
import os
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector
from app.connectors.observability.fake import SAMPLE_END
from app.db.base import utc_now
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.executor.service import ExecutionStore
from app.learning.demo import seed_incident
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.embedding import embedding_client
from app.runbooks.lifecycle import RunbookLifecycle, task_runbook_context
from app.runbooks.maturity import content_hash
from app.runbooks.maturity_scenario import create_sample, human_review, prepare_trial
from app.runbooks.models import Runbook
from app.runbooks.schemas import RunbookMaturity, RunbookView
from app.runbooks.service import RunbookService
from app.tasks.activities import TaskActivityStore
from app.tasks.approval.service import check_runbook
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus, VerificationRequired
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import ApprovalResponse, TransitionRequest, WorkflowInput
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.verification_runtime import fake_verification_registry
from app.verifier.activities import VerifierActivities
from app.verifier.models import ResourceExpectation, VerificationRequest, VerificationResult
from app.verifier.scenario import sample_spec
from tests.database_support import migrate
from tests.test_approval_integration import wait_prompt
from tests.test_executor_integration import ready
from tests.test_runbooks_integration import database, migrated_schema
from tests.test_verifier_integration import verify

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="运行 check-maturity.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需要本机 Temporal"
)


def settings() -> Settings:
    return Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})


async def read(database: Database, runbook_id: UUID) -> RunbookView:
    async with database.session() as session:
        return await RunbookService(
            session, lambda request: embedding_client(settings(), request)
        ).get(runbook_id)


async def reviewed(database: Database) -> RunbookView:
    return await human_review(database, settings(), await create_sample(database, settings()))


async def test_default_lifecycle_all_six_levels_and_downgrade_with_real_verifier(
    database: Database,
) -> None:
    guide = await create_sample(database, settings())
    assert guide.maturity is RunbookMaturity.DRAFT
    guide = await human_review(database, settings(), guide)
    assert guide.maturity is RunbookMaturity.REVIEWED
    stages = {
        3: RunbookMaturity.VERIFIED,
        5: RunbookMaturity.SEMI_AUTOMATED,
        10: RunbookMaturity.APPROVAL_AUTOMATED,
        20: RunbookMaturity.SELF_HEALING,
    }
    for count in range(1, 21):
        trial = await prepare_trial(database, settings(), guide.id)
        result = await verify(database, trial)
        assert result.task.status is TaskStatus.RESOLVED
        current = await read(database, guide.id)
        assert current.success_count == count and current.failure_count == 0
        if count in stages:
            assert current.maturity is stages[count]
    async with database.session() as session, session.begin():
        context = await RunbookLifecycle(session).context(current)
        assert context.trusted and context.maturity == "self_healing"
    for failure in range(1, 3):
        await verify(database, await prepare_trial(database, settings(), guide.id), recovered=False)
        current = await read(database, guide.id)
        assert current.failure_count == failure
        assert current.maturity is (
            RunbookMaturity.APPROVAL_AUTOMATED if failure == 1 else RunbookMaturity.REVIEWED
        )
    assert current.confidence == pytest.approx(21 / 24)
    async with database.session() as session, session.begin():
        context = await RunbookLifecycle(session).context(current)
        assert not context.trusted and context.review_evidence_id is None


async def test_concurrent_and_lost_response_verification_counts_once(database: Database) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    first, second = await asyncio.gather(verify(database, trial), verify(database, trial))
    assert first == second == await verify(database, trial, recovered=False)
    assert (await read(database, guide.id)).success_count == 1
    async with database.session() as session:
        records = await RunbookLifecycle(session).records(guide.id)
        outcomes = [e for e in records if e.parameters.get("operation") == "outcome"]
        assert len(outcomes) == 1 and outcomes[0].parameters["reference_id"] == first.evidence_id
        assert "config" not in json.dumps(outcomes[0].result_snapshot)
        assert len(str(outcomes[0].parameters["criteria_hash"])) == 64


async def test_external_actor_cannot_credit_verification(database: Database) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    result = await verify(database, trial)
    async with database.session() as session, session.begin():
        task = await session.get(AITask, UUID(trial.task_id))
        assert task
        with pytest.raises(VerificationRequired):
            await RunbookLifecycle(session).record_verification(task, UUID(result.evidence_id))
    assert (await read(database, guide.id)).success_count == 1


async def test_policy_denied_fact_does_not_count_as_runbook_failure(database: Database) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    config = Settings(
        APP_ENV="test",
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "deny",
                    "risk_levels": ["L0"],
                    "action_names": ["query_metrics"],
                    "decision": "deny",
                    "reason": "禁止查询",
                }
            ]
        },
    )
    await verify(database, trial, settings=config)
    current = await read(database, guide.id)
    assert current.success_count == current.failure_count == 0


async def test_maturity_audit_failure_rolls_back_counters_and_verification(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> object:
        if kwargs.get("operation") == "runbook.lifecycle":
            raise RuntimeError("成熟度审计失败")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError, match="成熟度审计失败"):
        await verify(database, trial)
    assert (await read(database, guide.id)).success_count == 0
    async with database.session() as session:
        task = await session.get(AITask, UUID(trial.task_id))
        assert task and task.status is TaskStatus.VERIFYING
        assert not await session.scalar(
            select(Evidence).where(
                Evidence.task_id == task.id, Evidence.source_tool == "verify_action"
            )
        )
    monkeypatch.setattr(LedgerService, "append_audit", original)
    await verify(database, trial)
    assert (await read(database, guide.id)).success_count == 1


async def test_content_change_resets_trust_and_old_inflight_result_is_not_credited(
    database: Database,
) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    async with database.session() as session, session.begin():
        draft = await session.get(Runbook, guide.id)
        assert draft
        from app.runbooks.scenario import payment_runbook

        updated = await RunbookService(
            session, lambda request: embedding_client(settings(), request)
        ).update(
            guide.id, payment_runbook(guide.name).model_copy(update={"description": "新诊断方案"})
        )
        assert updated.content_version == 2 and updated.maturity is RunbookMaturity.DRAFT
    await verify(database, trial)
    assert (await read(database, guide.id)).success_count == 0
    async with database.session() as session, session.begin():
        with pytest.raises(ValueError, match="内容已变化"):
            await RunbookLifecycle(session).context(guide)


async def test_review_is_actor_audited_idempotent_and_conflicting_decision_rejected(
    database: Database,
) -> None:
    guide = await create_sample(database, settings())
    request_id = uuid4()
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="审核", reason="Fake"
        )
        lifecycle = RunbookLifecycle(session)
        review = partial(
            lifecycle.review,
            guide.id,
            task_id=task.id,
            request_id=request_id,
            expected_revision=content_hash(guide),
            actor=" local-owner ",
        )
        first = await review(approved=True)
        second = await review(approved=True)
        assert first == second
        with pytest.raises(ValueError, match="冲突"):
            await review(approved=False)
        assert len(await lifecycle.records(guide.id)) == 1
        audits = await LedgerService(session).audits_for_task(task.id)
        assert any(a.actor == "local-owner" and a.operation == "runbook.lifecycle" for a in audits)


async def test_old_approved_plan_cannot_execute_after_new_runbook_binding(
    database: Database,
) -> None:
    guide = await reviewed(database)
    request = await ready(database)
    # 既有计划来自自主调查，不带 Runbook；同任务后来出现绑定后也不能沿用旧授权。
    trial = await prepare_trial(database, settings(), guide.id)
    async with database.session() as session, session.begin():
        match = next(
            e
            for e in await LedgerService(session).evidence_for_task(UUID(trial.task_id))
            if e.source_tool == "runbook.match"
        )
        await LedgerService(session).append_evidence(
            task_id=UUID(request.task.task_id),
            source_tool="runbook.match",
            parameters=match.parameters,
            result_snapshot=match.result_snapshot,
        )
    connector = FakeKubernetesWriteConnector()
    with pytest.raises(ValueError, match="Runbook"):
        await ExecutionStore(database, settings(), connector).execute(request)
    assert connector.issue_count == connector.execution_count == 0


async def test_runbook_context_in_plan_is_rechecked_after_review_revocation(
    database: Database,
) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    async with database.session() as session, session.begin():
        context = await task_runbook_context(
            session, UUID(trial.task_id), settings().runbook_maturity_config
        )
        assert context and context.maturity == "reviewed"
    request = await ready(database)
    async with database.session() as session, session.begin():
        record = await LedgerService(session).get_evidence(UUID(request.plan_evidence_id))
        plan = ActionPlan.model_validate_json(json.dumps(record.result_snapshot))
        contextual = plan.model_copy(update={"task_id": UUID(trial.task_id), "runbook": context})
        await check_runbook(session, contextual, settings())
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="撤回审核", reason="Fake"
        )
        await RunbookLifecycle(session).review(
            guide.id,
            task_id=task.id,
            request_id=uuid4(),
            expected_revision=content_hash(guide),
            actor="local-owner",
            approved=False,
        )
    async with database.session() as session, session.begin():
        with pytest.raises(ValueError, match="失效"):
            await check_runbook(session, contextual, settings())


async def test_replay_does_not_increment_maturity(database: Database) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    result = await verify(database, trial)
    config = settings()
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        record = await ledger.get_evidence(UUID(result.evidence_id))
        async with fake_verification_registry(config, session, recovered=False) as registry:
            replay = await ToolDispatcher(registry, create_policy_engine(config), ledger).dispatch(
                task_id=UUID(trial.task_id),
                tool_name="verify_action",
                parameters=record.parameters,
                actor="replay",
                mode=DispatchMode.REPLAY,
                replay_evidence_id=record.id,
                replay_before=utc_now(),
            )
        assert replay.status is DispatchStatus.REPLAYED
    assert (await read(database, guide.id)).success_count == 1


async def test_migration_rejects_loss_of_versioned_evidence(database: Database) -> None:
    await reviewed(database)
    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        migrate("downgrade", "0012_postmortem")
    migrate("check")


async def test_autonomous_reinvestigation_does_not_credit_previous_runbook(
    database: Database,
) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    failed = await verify(database, trial, recovered=False)
    snapshot = failed.task
    for status in (TaskStatus.RCA, TaskStatus.PLANNING, TaskStatus.EXECUTING, TaskStatus.VERIFYING):
        snapshot = await TaskActivityStore(database).transition(
            TransitionRequest(snapshot, status, "新一轮自主调查和人工处置")
        )
    await verify(database, snapshot)
    current = await read(database, guide.id)
    assert current.success_count == 0 and current.failure_count == 1
    async with database.session() as session, session.begin():
        context = await task_runbook_context(
            session, UUID(trial.task_id), settings().runbook_maturity_config
        )
        assert context is None


async def test_deleted_runbook_does_not_block_independent_verification(database: Database) -> None:
    guide = await reviewed(database)
    trial = await prepare_trial(database, settings(), guide.id)
    async with database.session() as session, session.begin():
        await RunbookService(session, lambda r: embedding_client(settings(), r)).delete(guide.id)
    assert (await verify(database, trial)).task.status is TaskStatus.RESOLVED


async def test_content_revert_has_new_version_and_does_not_reuse_old_review(
    database: Database,
) -> None:
    guide = await reviewed(database)
    from app.runbooks.scenario import payment_runbook

    async with database.session() as session, session.begin():
        service = RunbookService(session, lambda r: embedding_client(settings(), r))
        original = payment_runbook(guide.name)
        await service.update(guide.id, original.model_copy(update={"description": "新方案"}))
        reverted = await service.update(guide.id, original)
        assert reverted.content_version == 3 and reverted.maturity is RunbookMaturity.DRAFT
        context = await RunbookLifecycle(session).context(reverted)
        assert context.review_evidence_id is None and not context.trusted


async def test_crud_cannot_overwrite_managed_maturity(database: Database) -> None:
    guide = await reviewed(database)
    from app.runbooks.schemas import RunbookDraft

    draft = RunbookDraft.model_validate_json(
        json.dumps(
            {field: guide.model_dump(mode="json")[field] for field in RunbookDraft.model_fields}
        )
    )
    async with database.session() as session, session.begin():
        with pytest.raises(ValueError, match="只能由审核"):
            await RunbookService(session, lambda r: embedding_client(settings(), r)).update(
                guide.id,
                draft.model_copy(
                    update={"success_count": 999, "maturity": RunbookMaturity.SELF_HEALING}
                ),
            )


async def test_live_diagnostic_failures_degrade_and_retry_counts_once(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.activities import AgentActivities
    from app.agent.investigation import InvestigationSpec
    from app.agent.workflow_models import InvestigationRequest
    from app.connectors.observability.fake import FakePrometheusConnector

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Fake 指标诊断失败")

    monkeypatch.setattr(FakePrometheusConnector, "query_metrics", fail)
    guide = await reviewed(database)
    for count in (1, 2):
        trial = await prepare_trial(database, settings(), guide.id, until=TaskStatus.INVESTIGATING)
        async with database.session() as session:
            record = next(
                e
                for e in await LedgerService(session).evidence_for_task(UUID(trial.task_id))
                if e.source_tool == "runbook.match"
            )
            assert isinstance(record.result_snapshot, dict)
            guide_json = str(record.result_snapshot["runbook_json"])
        window = sample_spec(trial)
        spec = InvestigationSpec(
            service_name=window.service_name,
            title="payment-service 5xx 试用",
            start=window.start,
            end=window.end,
        )
        request = InvestigationRequest(trial, spec.model_dump_json(), guide_json)
        for _ in range(2):
            with pytest.raises(ApplicationError):
                await AgentActivities(database, settings()).investigate(request)
        current = await read(database, guide.id)
        assert current.failure_count == count and current.success_count == 0
    async with database.session() as session, session.begin():
        context = await RunbookLifecycle(session).context(current)
        assert context.review_evidence_id is None and not context.trusted


@local_temporal
async def test_primary_workflow_runbook_plan_approval_execution_and_verifier_credit(
    database: Database,
) -> None:
    from datetime import timedelta

    config = Settings(
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
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"maturity-primary-{uuid4().hex}",
        },
    )
    guide = await human_review(database, config, await create_sample(database, config))
    snapshot, spec = await seed_incident(database, config)
    connector = FakeKubernetesWriteConnector(clock=lambda: SAMPLE_END)
    verifier = VerifierActivities(
        database,
        config,
        registry_factory=partial(
            fake_verification_registry,
            window_start=SAMPLE_END,
            window_end=SAMPLE_END + timedelta(minutes=5),
        ),
    )
    client = await Client.connect(config.temporal_config.address)
    handle = None
    completed_report = None
    try:
        async with create_worker(
            client,
            database,
            config,
            verifier_activities=verifier,
            executor_activities=ExecutorActivities(database, config, connector=connector),
        ):
            handle = await start_task_workflow(
                client,
                WorkflowInput(
                    snapshot.task_id,
                    investigation_json=spec.model_dump_json(),
                    execution_enabled=True,
                ),
                task_queue=config.temporal_config.task_queue,
            )
            prompt = await wait_prompt(handle)
            await handle.signal(
                AITaskWorkflow.approve_actions,
                ApprovalResponse(
                    snapshot.task_id,
                    prompt.approval_id,
                    prompt.task.version,
                    prompt.action_hash,
                    "approved",
                    "local-owner",
                ),
            )
            result = await asyncio.wait_for(handle.result(), 45)
            completed_report = result.postmortem_json
        assert result.task and result.task.status is TaskStatus.CLOSED
        assert (
            connector.execution_count == 1 and (await read(database, guide.id)).success_count == 1
        )
        plan = ActionPlan.model_validate_json(result.action_plan_json or "{}")
        assert plan.runbook and plan.runbook.runbook_id == guide.id
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if handle is not None:
            from temporalio.client import WorkflowExecutionStatus

            if completed_report:
                from app.learning.models import IncidentReport

                for task_id in IncidentReport.model_validate_json(
                    completed_report
                ).improvement_task_ids:
                    child = client.get_workflow_handle(f"ai-task-{task_id}")
                    if (await child.describe()).status is WorkflowExecutionStatus.RUNNING:
                        await child.terminate("Step 35 改进任务验收清理")
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 35 主链路验收清理")
        await connector.aclose()


@local_temporal
async def test_temporal_commit_lost_response_worker_restart_and_replay(database: Database) -> None:
    config = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"maturity-{uuid4().hex}",
        },
    )
    guide = await human_review(database, config, await create_sample(database, config))
    trial = await prepare_trial(database, config, guide.id)
    committed = asyncio.Event()

    class LoseResponse(VerifierActivities):
        @activity.defn(name="verifier.verify_action")
        async def verify(self, request: VerificationRequest) -> VerificationResult:
            await super().verify(request)
            committed.set()
            raise ApplicationError("验证和计数已提交但响应丢失")

    client = await Client.connect(config.temporal_config.address)
    verifier = LoseResponse(database, config, registry_factory=fake_verification_registry)
    handle = None
    try:
        async with create_worker(client, database, config, verifier_activities=verifier):
            handle = await start_task_workflow(
                client,
                WorkflowInput(
                    trial.task_id,
                    verification_json=sample_spec(trial).model_dump_json(),
                    postmortem_enabled=False,
                ),
                task_queue=config.temporal_config.task_queue,
            )
            await asyncio.wait_for(committed.wait(), 20)
        assert (await read(database, guide.id)).success_count == 1
        normal = VerifierActivities(database, config, registry_factory=fake_verification_registry)
        async with create_worker(client, database, config, verifier_activities=normal):
            result = await asyncio.wait_for(handle.result(), 30)
        assert result.task and result.task.status is TaskStatus.RESOLVED
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
        assert (await read(database, guide.id)).success_count == 1
    finally:
        if handle is not None:
            from temporalio.client import WorkflowExecutionStatus

            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 35 验收清理")
