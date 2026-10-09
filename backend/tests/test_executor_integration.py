"""本机隔离 PostgreSQL/Temporal：授权、审计、幂等、失败与恢复回放。"""

import asyncio
import json
import os
from dataclasses import replace
from uuid import UUID

import pytest
from temporalio.client import Client
from temporalio.worker import Replayer

from app.agent.client import LLMClient
from app.agent.fake import ChatStep, FakeLLM
from app.agent.models import ChatRequest
from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.executor.models import ExecutionCommand, ExecutionRequest, ExecutionResult
from app.executor.service import ExecutionStore
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.activities import TaskActivityStore
from app.tasks.approval.service import ApprovalStore
from app.tasks.models import AITask
from app.tasks.planning.activities import PlanningActivities
from app.tasks.planning.models import ActionPlanDraft
from app.tasks.planning.scenario import payment_plan_response
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import TransitionRequest, WorkflowInput
from tests.test_action_plans_integration import planning
from tests.test_approval_integration import answer, signal_value, wait_prompt, waiting
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import new_task, runtime_settings
from tests.test_reviewer_integration import database, migrated_schema

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-executor.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


def settings() -> Settings:
    return Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})


async def ready(database: Database, approved: bool = True) -> ExecutionRequest:
    request, _ = await waiting(database)
    store = ApprovalStore(database, settings())
    prompt = await store.notify(request, FakeFeishuConnector())
    snapshot = request.task
    if approved:
        await store.decide(answer(prompt))
        snapshot = await TaskActivityStore(database).transition(
            TransitionRequest(snapshot, TaskStatus.EXECUTING, "Executor 精确审批交接")
        )
    return ExecutionRequest(snapshot, request.plan_evidence_id, prompt)


async def test_no_approval_l3_refused_without_connector_calls(database: Database) -> None:
    request = await ready(database, False)
    connector = FakeKubernetesWriteConnector()
    with pytest.raises(ValueError):
        await ExecutionStore(database, settings(), connector).execute(request)
    assert connector.execution_count == connector.issue_count == 0
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(request.task.task_id))
        assert any(
            a.event_type is AuditEventType.EXECUTION and a.outcome == "rejected" for a in audits
        )


async def test_approved_rollback_audit_no_secret_and_verifying_only(database: Database) -> None:
    request = await ready(database)
    connector = FakeKubernetesWriteConnector()
    store = ExecutionStore(database, settings(), connector)
    result = await store.execute(request)
    assert result.task.status is TaskStatus.VERIFYING
    assert connector.execution_count == 1 and connector.targets["payment-service"].image.endswith(
        ":v2.3.6"
    )
    assert await store.execute(request) == result and connector.issue_count == 1
    async with database.session() as session:
        ledger = LedgerService(session)
        evidence = await ledger.get_evidence(UUID(result.evidence_ids[0]))
        assert evidence.source_tool == "execute_action"
        audits = await ledger.audits_for_task(UUID(request.task.task_id))
        assert (
            len(
                [
                    a
                    for a in audits
                    if a.event_type is AuditEventType.EXECUTION and a.outcome == "succeeded"
                ]
            )
            == 1
        )
        assert any(a.operation == "execute_action" and a.evidence_id == evidence.id for a in audits)
        text = json.dumps(
            [e.result_snapshot for e in await ledger.evidence_for_task(evidence.task_id)]
        )
        for token in connector.credentials:
            assert token not in text
        assert (await session.get(AITask, evidence.task_id)).status is TaskStatus.VERIFYING  # type: ignore[union-attr]
    await connector.aclose()
    assert await store.replay(request, UUID(result.evidence_ids[0])) == evidence.result_snapshot
    assert connector.execution_count == 1


async def test_concurrent_execution_exactly_once(database: Database) -> None:
    request = await ready(database)
    connector = FakeKubernetesWriteConnector()
    store = ExecutionStore(database, settings(), connector)
    results = await asyncio.gather(store.execute(request), store.execute(request))
    assert results[0] == results[1] and connector.execution_count == connector.issue_count == 1


@pytest.mark.parametrize("name", ["restart_service", "scale_service", "rollback_prod"])
async def test_policy_allow_without_approval_and_three_actions_persist(
    database: Database, name: str
) -> None:
    config = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "fake-only",
                    "risk_levels": ["L3"],
                    "decision": "allow",
                    "reason": "Fake 验收",
                    "environments": ["test"],
                }
            ]
        },
    )
    request = await planning(database)

    def factory(chat: ChatRequest) -> LLMClient:
        response = payment_plan_response(chat)
        draft = ActionPlanDraft.model_validate_json(response.message.content or "{}")
        params = {
            "restart_service": {"strategy": "rolling"},
            "scale_service": {"from_replicas": 3, "to_replicas": 5},
            "rollback_prod": {"from_version": "v2.3.7", "to_version": "v2.3.6"},
        }[name]
        draft = draft.model_copy(
            update={
                "actions": (
                    draft.actions[0].model_copy(update={"name": name, "parameters": params}),
                )
            }
        )
        return FakeLLM(
            [
                ChatStep(
                    chat,
                    response.model_copy(
                        update={
                            "message": response.message.model_copy(
                                update={"content": draft.model_dump_json()}
                            )
                        }
                    ),
                )
            ]
        )

    result = await PlanningActivities(database, config, llm_factory=factory).plan(request)
    snapshot = await TaskActivityStore(database).transition(
        TransitionRequest(request.task, TaskStatus.EXECUTING, "Fake Policy 自动放行")
    )
    connector = FakeKubernetesWriteConnector()
    outcome = await ExecutionStore(database, config, connector).execute(
        ExecutionRequest(snapshot, result.evidence_id)
    )
    assert outcome.task.status is TaskStatus.VERIFYING and connector.execution_count == 1
    assert connector.targets["payment-service"].replicas == (5 if name == "scale_service" else 3)
    assert connector.targets["payment-service"].image.endswith(
        ":v2.3.6" if name == "rollback_prod" else ":v2.3.7"
    )


@pytest.mark.parametrize("recovered", [True, False])
async def test_independent_verifier_inherits_actual_execution_target(
    database: Database, recovered: bool
) -> None:
    from datetime import timedelta
    from functools import partial

    from app.connectors.kubernetes.execution import ExecutionReceipt
    from app.tools.verification_runtime import fake_verification_registry
    from app.verifier.activities import VerifierActivities
    from app.verifier.models import VerificationRequest
    from app.verifier.scenario import sample_spec

    request = await ready(database)
    result = await ExecutionStore(database, settings(), FakeKubernetesWriteConnector()).execute(
        request
    )
    async with database.session() as session:
        record = await LedgerService(session).get_evidence(UUID(result.evidence_ids[0]))
        receipt = ExecutionReceipt.model_validate_json(json.dumps(record.result_snapshot))
        command = ExecutionCommand.model_validate_json(json.dumps(record.parameters))
    spec = sample_spec(result.task).model_copy(
        update={
            "action_id": command.action_id,
            "action_completed_at": receipt.completed_at,
            "start": receipt.completed_at,
            "end": receipt.completed_at + timedelta(minutes=5),
        }
    )
    verifier = VerifierActivities(
        database,
        settings(),
        registry_factory=partial(
            fake_verification_registry,
            recovered=recovered,
            window_start=spec.start,
            window_end=spec.end,
        ),
    )
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError):
        await verifier.verify(
            VerificationRequest(
                result.task,
                spec.model_copy(update={"expected_image": "unapproved:v1"}).model_dump_json(),
            )
        )
    outcome = await verifier.verify(VerificationRequest(result.task, spec.model_dump_json()))
    assert outcome.task.status is (TaskStatus.RESOLVED if recovered else TaskStatus.INVESTIGATING)


@pytest.mark.parametrize("tamper", ["version", "task", "plan", "approval"])
async def test_forged_or_stale_request_refused(database: Database, tamper: str) -> None:
    from uuid import uuid4

    request = await ready(database)
    if tamper == "version":
        request = replace(request, task=replace(request.task, version=request.task.version + 1))
    elif tamper == "task":
        request = replace(request, task=await new_task(database))
    elif tamper == "plan":
        request = replace(request, plan_evidence_id=str(uuid4()))
    else:
        request = replace(request, approval=None)
    connector = FakeKubernetesWriteConnector()
    with pytest.raises((ValueError, LookupError, PermissionError)):
        await ExecutionStore(database, settings(), connector).execute(request)
    assert connector.execution_count == connector.issue_count == 0


async def test_changed_policy_invalidates_approved_plan(database: Database) -> None:
    request = await ready(database)
    changed = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        POLICY_CONFIG={
            "rules": [
                {"id": "new-deny", "risk_levels": ["L3"], "decision": "deny", "reason": "禁止变更"}
            ]
        },
    )
    connector = FakeKubernetesWriteConnector()
    with pytest.raises(ValueError, match="Policy"):
        await ExecutionStore(database, changed, connector).execute(request)
    assert connector.execution_count == connector.issue_count == 0


async def test_precondition_changed_refused_before_credentials(database: Database) -> None:
    request = await ready(database)
    connector = FakeKubernetesWriteConnector()
    connector.targets["payment-service"] = connector.targets["payment-service"].model_copy(
        update={"image": "registry.example.invalid/payment:v2.3.6"}
    )
    with pytest.raises(ValueError, match="当前镜像"):
        await ExecutionStore(database, settings(), connector).execute(request)
    assert connector.execution_count == connector.issue_count == 0


async def test_modified_command_cannot_use_executor_scope(database: Database) -> None:
    request = await ready(database)
    connector = FakeKubernetesWriteConnector()
    store = ExecutionStore(database, settings(), connector)
    intent = await store.intent(request, 0)
    changed = intent.model_copy(
        update={"expected_image": "registry.example.invalid/payment:v2.3.5"}
    )
    with pytest.raises(PermissionError, match="执行意图"):
        await store.execute_one(request, changed)
    async with database.session() as session, session.begin():
        result = await store.dispatcher(session).dispatch(
            task_id=UUID(request.task.task_id),
            tool_name="execute_action",
            parameters=json.loads(intent.model_dump_json()),
            actor="agent",
        )
        assert result.error_code == "approval_required"
    assert connector.execution_count == connector.issue_count == 0


async def test_disabled_host_gate_covers_direct_methods(database: Database) -> None:
    request = await ready(database)
    connector = FakeKubernetesWriteConnector()
    store = ExecutionStore(database, Settings(APP_ENV="test"), connector)
    with pytest.raises(PermissionError, match="启用"):
        await store.intent(request, 0)
    assert connector.issue_count == connector.execution_count == 0


async def test_changed_host_binding_invalidates_pending_intent(database: Database) -> None:
    from app.connectors.kubernetes.execution import fake_binding

    request = await ready(database)
    connector = FakeKubernetesWriteConnector()
    store = ExecutionStore(database, settings(), connector)
    intent = await store.intent(request, 0)
    binding = fake_binding()
    binding = binding.model_copy(
        update={
            "images": {
                **binding.images,
                "v2.3.6": "registry.example.invalid/payment:changed",
            }
        }
    )
    changed = Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True, "bindings": (binding,)})
    with pytest.raises(PermissionError, match="绑定变化"):
        await ExecutionStore(database, changed, connector).execute_one(request, intent)
    assert connector.issue_count == connector.execution_count == 0


async def test_verifier_cannot_confirm_an_unexecuted_action_plan(database: Database) -> None:
    from temporalio.exceptions import ApplicationError

    from app.verifier.activities import VerifierActivities
    from app.verifier.models import VerificationRequest
    from app.verifier.scenario import sample_spec

    request = await ready(database)
    snapshot = await TaskActivityStore(database).transition(
        TransitionRequest(request.task, TaskStatus.VERIFYING, "验证入口不能替代真正的执行回执")
    )
    with pytest.raises(ApplicationError):
        await VerifierActivities(database, settings()).verify(
            VerificationRequest(snapshot, sample_spec(snapshot).model_dump_json())
        )


@local_temporal
async def test_temporal_execution_commit_response_loss_never_reexecutes(database: Database) -> None:
    from temporalio import activity
    from temporalio.exceptions import ApplicationError

    class LoseResponse(ExecutorActivities):
        calls = 0

        @activity.defn(name="executor.execute_action")
        async def execute(self, request: ExecutionRequest) -> "ExecutionResult":
            result = await super().execute(request)
            self.calls += 1
            if self.calls == 1:
                raise ApplicationError("执行已提交但响应丢失")
            return result

    base = runtime_settings()
    config = base.model_copy(update={"execution_config": settings().execution_config})
    connector = FakeKubernetesWriteConnector()
    executor = LoseResponse(database, config, connector=connector)
    client = await Client.connect(config.temporal_config.address)
    task = await new_task(database)
    async with create_worker(client, database, config, executor_activities=executor):
        handle = await start_task_workflow(
            client,
            WorkflowInput(
                task.task_id,
                investigation_json=SPEC.model_dump_json(),
                execution_enabled=True,
                postmortem_enabled=False,
            ),
            task_queue=config.temporal_config.task_queue,
        )
        prompt = await wait_prompt(handle)
        await handle.signal(AITaskWorkflow.approve_actions, signal_value(prompt))
        result = await asyncio.wait_for(handle.result(), 30)
    assert result.task and result.task.status is TaskStatus.VERIFYING
    assert executor.calls == 2 and connector.execution_count == connector.issue_count == 1
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


async def test_audit_failure_after_side_effect_reuses_durable_intent(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await ready(database)
    connector = FakeKubernetesWriteConnector()
    store = ExecutionStore(database, settings(), connector)
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> object:
        if kwargs.get("operation") == "rollback_prod":
            raise RuntimeError("执行回执保存失败")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError):
        await store.execute(request)
    assert connector.execution_count == 1
    async with database.session() as session:
        records = await LedgerService(session).evidence_for_task(UUID(request.task.task_id))
        assert len([e for e in records if e.source_tool == "execution.intent"]) == 1
        assert not any(e.source_tool == "execute_action" for e in records)
    monkeypatch.setattr(LedgerService, "append_audit", original)
    result = await store.execute(request)
    assert result.task.status is TaskStatus.VERIFYING and connector.execution_count == 1


@local_temporal
@pytest.mark.parametrize("decision", ["approved", "rejected", "expired"])
async def test_temporal_restart_execute_and_replay(database: Database, decision: str) -> None:
    base = runtime_settings()
    config = base.model_copy(update={"execution_config": settings().execution_config})
    client = await Client.connect(config.temporal_config.address)
    connector = FakeKubernetesWriteConnector()
    executor = ExecutorActivities(database, config, connector=connector)
    task = await new_task(database)
    async with create_worker(
        client, database, config, executor_activities=executor, max_cached_workflows=0
    ):
        handle = await start_task_workflow(
            client,
            WorkflowInput(
                task.task_id,
                investigation_json=SPEC.model_dump_json(),
                execution_enabled=True,
                postmortem_enabled=False,
                human_timeout_seconds=0.1 if decision == "expired" else 60,
            ),
            task_queue=config.temporal_config.task_queue,
        )
        prompt = await wait_prompt(handle)
    if decision != "expired":
        await handle.signal(AITaskWorkflow.approve_actions, signal_value(prompt, decision))
    async with create_worker(
        client, database, config, executor_activities=executor, max_cached_workflows=0
    ):
        result = await asyncio.wait_for(handle.result(), 30)
    assert result.task and result.task.status is (
        TaskStatus.VERIFYING if decision == "approved" else TaskStatus.ESCALATED
    )
    assert connector.execution_count == (1 if decision == "approved" else 0)
    async with database.session() as session:
        history = await TaskService(session).history(UUID(task.task_id))
        assert [(h.to_status, h.sequence) for h in history] == [
            (h.status, h.version) for h in result.history
        ]
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
