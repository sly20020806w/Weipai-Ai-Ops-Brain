"""隔离 PostgreSQL/Temporal：六类停止、通知、幂等、失败事务与写入门禁。"""

import asyncio
import os
from dataclasses import replace
from functools import partial
from uuid import UUID

import pytest
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.feishu.base import FeishuError
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.feishu.models import Notification, NotificationReceipt
from app.connectors.kubernetes.execution import (
    ActionCredential,
    ExecutionReceipt,
    FakeKubernetesWriteConnector,
)
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.executor.models import ExecutionCommand
from app.executor.service import ExecutionStore
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.tasks.activities import TaskActivityStore
from app.tasks.models import AITask
from app.tasks.safety.activities import SafetyActivities
from app.tasks.safety.models import AbortReason, AutomationAborted, SafetyConfig
from app.tasks.safety.scenario import seed_case
from app.tasks.safety.service import SafetyStore
from app.tasks.service import TaskService, TaskStateConflict
from app.tasks.states import TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TransitionRequest, WorkflowInput
from app.tools.models import DispatchStatus
from app.tools.verification_runtime import fake_verification_registry
from app.verifier.activities import VerifierActivities
from app.verifier.models import VerificationReport, VerificationRequest
from app.verifier.scenario import sample_spec
from tests.test_approval_integration import signal_value, wait_prompt
from tests.test_executor_integration import ready
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import new_task, runtime_settings, seed
from tests.test_reviewer_integration import database, migrated_schema
from tests.test_verifier_integration import verifying_task

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-safety.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


@pytest.mark.parametrize("reason", list(AbortReason))
async def test_each_condition_persists_aborted_blocks_approved_action_and_notifies(
    database: Database, reason: AbortReason
) -> None:
    request = await ready(database)
    async with database.session() as session, session.begin():
        await seed_case(session, UUID(request.task.task_id), reason)
    config = Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})
    connector, feishu = FakeKubernetesWriteConnector(), FakeFeishuConnector()
    safety = SafetyActivities(database, config, connector=feishu)
    result = await safety.check(request.task)
    assert result.task.status is TaskStatus.AUTOMATION_ABORTED and result.reasons == (reason.value,)
    assert await safety.check(request.task) == result
    first = await safety.notify(result.task.task_id)
    assert await safety.notify(result.task.task_id) == first and len(feishu.sent_messages) == 1
    message = feishu.sent_messages[0].notification.model_dump_json()
    assert result.evidence_id is not None
    assert result.evidence_id in message and "人工接管" in message
    with pytest.raises(AutomationAborted):
        await ExecutionStore(database, config, connector).execute(request)
    assert connector.issue_count == connector.execution_count == 0
    async with database.session() as session:
        history = await TaskService(session).history(UUID(request.task.task_id))
        assert history[-1].to_status is TaskStatus.AUTOMATION_ABORTED
        ledger = LedgerService(session)
        abort = await ledger.get_evidence(UUID(result.evidence_id or ""))
        assert isinstance(abort.result_snapshot, dict)
        assert "config_hash" in abort.result_snapshot and "max_actions" not in str(
            abort.result_snapshot
        )
        audits = await ledger.audits_for_task(UUID(request.task.task_id))
        assert len([a for a in audits if a.operation == "safety.abort"]) == 1
        assert len([a for a in audits if a.operation == "safety.notify"]) == 1


async def test_concurrent_checks_and_notifications_commit_once(database: Database) -> None:
    request = await ready(database)
    async with database.session() as session, session.begin():
        await seed_case(session, UUID(request.task.task_id), AbortReason.METRICS_WORSENING)
    store = SafetyStore(database, SafetyConfig())
    results = await asyncio.gather(store.check(request.task), store.check(request.task))
    assert results[0] == results[1]
    feishu = FakeFeishuConnector()
    safety = SafetyActivities(database, Settings(APP_ENV="test"), connector=feishu)
    notifications = await asyncio.gather(
        safety.notify(request.task.task_id), safety.notify(request.task.task_id)
    )
    assert notifications[0] == notifications[1] and len(feishu.sent_messages) == 1


async def test_stale_snapshot_and_other_task_facts_do_not_abort(database: Database) -> None:
    first, other = await ready(database), await ready(database)
    async with database.session() as session, session.begin():
        await seed_case(session, UUID(other.task.task_id), AbortReason.METRICS_WORSENING)
    store = SafetyStore(database, SafetyConfig())
    assert (await store.check(first.task)).evidence_id is None
    with pytest.raises(TaskStateConflict):
        await store.check(replace(first.task, version=first.task.version - 1))


async def test_abort_audit_failure_rolls_back_state_history_and_evidence(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await ready(database)
    async with database.session() as session, session.begin():
        await seed_case(session, UUID(request.task.task_id), AbortReason.METRICS_WORSENING)
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> object:
        if kwargs.get("operation") == "safety.abort":
            raise RuntimeError("Fake 审计失败")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError):
        await SafetyStore(database, SafetyConfig()).check(request.task)
    async with database.session() as session:
        task = await session.get(AITask, UUID(request.task.task_id))
        assert (
            task
            and task.status is TaskStatus.EXECUTING
            and task.status_version == request.task.version
        )
        assert not any(
            e.source_tool == "safety.abort"
            for e in await LedgerService(session).evidence_for_task(task.id)
        )


async def test_send_then_transaction_failure_reuses_notification_id(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await ready(database)
    async with database.session() as session, session.begin():
        await seed_case(session, UUID(request.task.task_id), AbortReason.METRICS_WORSENING)
    feishu = FakeFeishuConnector()
    safety = SafetyActivities(database, Settings(APP_ENV="test"), connector=feishu)
    result = await safety.check(request.task)
    original = LedgerService.append_evidence

    async def fail(self: LedgerService, **kwargs: object) -> Evidence:
        if kwargs.get("source_tool") == "safety.notification":
            raise RuntimeError("Fake 发送后事务失败")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(RuntimeError):
        await safety.notify(request.task.task_id)
    assert (await safety.check(request.task)).task == result.task
    monkeypatch.setattr(LedgerService, "append_evidence", original)
    await safety.notify(request.task.task_id)
    assert len(feishu.sent_messages) == 1


async def test_persistent_latch_rejects_old_approval_after_escalation_and_dispatcher(
    database: Database,
) -> None:
    request = await ready(database)
    async with database.session() as session, session.begin():
        await seed_case(session, UUID(request.task.task_id), AbortReason.METRICS_WORSENING)
    store = SafetyStore(database, SafetyConfig())
    aborted = await store.check(request.task)
    await TaskActivityStore(database).transition(
        TransitionRequest(aborted.task, TaskStatus.ESCALATED, "人工接管记录")
    )
    connector = FakeKubernetesWriteConnector()
    executor = ExecutionStore(
        database, Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True}), connector
    )
    with pytest.raises(AutomationAborted):
        await executor.execute(request)
    async with database.session() as session, session.begin():
        result = await executor.dispatcher(session).dispatch(
            task_id=UUID(request.task.task_id),
            tool_name="execute_action",
            parameters={},
            actor="fake-stale-executor",
        )
        assert (
            result.status is DispatchStatus.REJECTED and result.error_code == "automation_aborted"
        )
    assert connector.issue_count == connector.execution_count == 0


async def test_budget_checked_before_new_intent_and_credentials(database: Database) -> None:
    request = await ready(database)
    connector = FakeKubernetesWriteConnector()
    config = Settings(
        APP_ENV="test", EXECUTION_CONFIG={"enabled": True}, SAFETY_CONFIG={"max_actions": 1}
    )
    async with database.session() as session, session.begin():
        await LedgerService(session).append_evidence(
            task_id=UUID(request.task.task_id),
            source_tool="execution.intent",
            parameters={"plan_evidence_id": "earlier-plan", "action_id": "earlier-action"},
            result_snapshot={"execution_id": "fake-prior-attempt"},
        )
    with pytest.raises(AutomationAborted):
        await ExecutionStore(database, config, connector).execute(request)
    assert connector.issue_count == connector.execution_count == 0
    assert (
        await SafetyStore(database, config.safety_config).check(request.task)
    ).task.status is TaskStatus.AUTOMATION_ABORTED


class FailingWriteConnector(FakeKubernetesWriteConnector):
    async def execute(
        self, command: ExecutionCommand, credential: ActionCredential
    ) -> ExecutionReceipt:
        raise RuntimeError("Fake 动作端失败")


@local_temporal
async def test_verifier_worsening_aborts_before_recovery_transition_and_retries_same_report(
    database: Database,
) -> None:
    snapshot = await verifying_task(database)
    async with database.session() as session, session.begin():
        await seed_case(session, UUID(snapshot.task_id), AbortReason.METRICS_WORSENING)
    config = runtime_settings()
    verifier = VerifierActivities(
        database, config, registry_factory=partial(fake_verification_registry, recovered=False)
    )
    feishu = FakeFeishuConnector()
    client = await Client.connect(config.temporal_config.address)
    spec_json = sample_spec(snapshot).model_dump_json()
    async with create_worker(
        client, database, config, verifier_activities=verifier, feishu_connector=feishu
    ):
        handle = await start_task_workflow(
            client,
            WorkflowInput(snapshot.task_id, verification_json=spec_json),
            task_queue=config.temporal_config.task_queue,
        )
        result = await asyncio.wait_for(handle.result(), 30)
    assert result.task and result.task.status is TaskStatus.AUTOMATION_ABORTED
    assert (
        result.verification_json
        and not VerificationReport.model_validate_json(result.verification_json).passed
    )
    assert result.takeover_notification_state == "sent" and len(feishu.sent_messages) == 1
    retried = await verifier.verify(VerificationRequest(snapshot, spec_json))
    assert retried.task == result.task and retried.evidence_id == result.verification_evidence_id
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


class FailingFeishuConnector(FakeFeishuConnector):
    async def send(self, notification: Notification) -> NotificationReceipt:
        if notification.msg_type == "text":
            raise FeishuError("Fake 通知失败")
        return await super().send(notification)


@local_temporal
@pytest.mark.parametrize("notification_fails", [False, True])
async def test_temporal_failures_stop_retries_notify_and_replay(
    database: Database, notification_fails: bool
) -> None:
    config = runtime_settings().model_copy(
        update={
            "execution_config": Settings(
                APP_ENV="test", EXECUTION_CONFIG={"enabled": True}
            ).execution_config
        }
    )
    await seed(database)
    client = await Client.connect(config.temporal_config.address)
    write = FailingWriteConnector()
    feishu = FailingFeishuConnector() if notification_fails else FakeFeishuConnector()
    executor = ExecutorActivities(database, config, connector=write)
    async with create_worker(
        client, database, config, executor_activities=executor, feishu_connector=feishu
    ):
        handle = await start_task_workflow(
            client,
            WorkflowInput(
                (await new_task(database)).task_id,
                investigation_json=SPEC.model_dump_json(),
                execution_enabled=True,
                activity_max_attempts=5,
            ),
            task_queue=config.temporal_config.task_queue,
        )
        prompt = await wait_prompt(handle)
        await handle.signal(AITaskWorkflow.approve_actions, signal_value(prompt))
        result = await asyncio.wait_for(handle.result(), 45)
    assert result.task and result.task.status is TaskStatus.AUTOMATION_ABORTED
    assert write.issue_count == 3 and write.execution_count == 0
    assert result.takeover_notification_state == ("failed" if notification_fails else "sent")
    assert len(feishu.sent_messages) == (1 if notification_fails else 2)  # 审批卡片与接管通知
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
