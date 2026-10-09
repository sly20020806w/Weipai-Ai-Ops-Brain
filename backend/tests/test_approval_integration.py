"""本机 PostgreSQL/Temporal：审批持久化、动作改动、拒绝超时与恢复回放。"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC
from uuid import UUID, uuid4

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.db.session import Database
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.activities import TaskActivityStore
from app.tasks.approval.activities import ApprovalActivities
from app.tasks.approval.models import action_hash
from app.tasks.approval.service import ApprovalStore
from app.tasks.planning.activities import PlanningActivities
from app.tasks.planning.models import ActionPlan
from app.tasks.safety.activities import SafetyActivities
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import (
    ApprovalDecisionRequest,
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalResult,
    HumanResponse,
    TransitionRequest,
    WorkflowInput,
    WorkflowProgress,
)
from tests.test_action_plans_integration import planning
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import new_task, runtime_settings, seed
from tests.test_reviewer_integration import database, migrated_schema

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-approval.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)


async def waiting(database: Database) -> tuple[ApprovalRequest, ActionPlan]:
    request = await planning(database)
    result = await PlanningActivities(database, Settings(APP_ENV="test")).plan(request)
    snapshot = await TaskActivityStore(database).transition(
        TransitionRequest(request.task, TaskStatus.WAITING_APPROVAL, "Step 30 验收")
    )
    return ApprovalRequest(snapshot, result.evidence_id), ActionPlan.model_validate_json(
        result.plan_json
    )


def answer(prompt: ApprovalPrompt, decision: str = "approved") -> ApprovalDecisionRequest:
    return ApprovalDecisionRequest(prompt, signal_value(prompt, decision))


def signal_value(prompt: ApprovalPrompt, decision: str = "approved") -> ApprovalResponse:
    return ApprovalResponse(
        prompt.task.task_id,
        prompt.approval_id,
        prompt.task.version,
        prompt.action_hash,
        decision,
        "local-owner",
    )


async def test_concurrent_approval_is_persistent_hash_bound_and_audited(database: Database) -> None:
    request, plan = await waiting(database)
    store, connector = ApprovalStore(database, Settings(APP_ENV="test")), FakeFeishuConnector()
    prompts = await asyncio.gather(
        store.notify(request, connector), store.notify(request, connector)
    )
    assert prompts[0] == prompts[1] and len(connector.sent_messages) == 1
    value = answer(prompts[0])
    assert value.response is not None
    value = replace(value, response=replace(value.response, actor=" local-owner "))
    results = await asyncio.gather(store.decide(value), store.decide(value))
    assert results[0] == results[1] and results[0].action_hash == action_hash(plan)
    assert await store.is_approved(prompts[0], plan)
    async with database.session() as session:
        ledger = LedgerService(session)
        evidence = await ledger.get_evidence(UUID(results[0].evidence_id))
        assert isinstance(evidence.result_snapshot, dict)
        assert evidence.result_snapshot["action_hash"] == prompts[0].action_hash
        audits = [
            a
            for a in await ledger.audits_for_task(plan.task_id)
            if a.event_type is AuditEventType.APPROVAL
        ]
        assert (
            len(audits) == 2
            and audits[-1].actor == "local-owner"
            and audits[-1].occurred_at.tzinfo is UTC
        )
    changed = plan.model_dump(mode="json")
    changed["actions"][0]["action"]["parameters"]["to_version"] = "v2.3.5"
    assert not await store.is_approved(
        prompts[0], ActionPlan.model_validate_json(json.dumps(changed))
    )
    await TaskActivityStore(database).transition(
        TransitionRequest(request.task, TaskStatus.EXECUTING, "批准交接")
    )
    assert await store.is_approved(prompts[0], plan)
    assert await store.decide(value) == results[0]


@pytest.mark.parametrize("decision", ["rejected", "expired"])
async def test_rejection_or_expiry_has_no_execution_authorization(
    database: Database, decision: str
) -> None:
    request, plan = await waiting(database)
    store = ApprovalStore(database, Settings(APP_ENV="test"))
    prompt = await store.notify(request, FakeFeishuConnector())
    result = await store.decide(
        ApprovalDecisionRequest(prompt) if decision == "expired" else answer(prompt, decision)
    )
    assert result.decision == decision and not await store.is_approved(prompt, plan)
    with pytest.raises(ValueError):
        await TaskActivityStore(database).transition(
            TransitionRequest(request.task, TaskStatus.EXECUTING, "不能执行")
        )
    with pytest.raises(ValueError, match="其他决定"):
        await store.decide(answer(prompt))


@pytest.mark.parametrize(
    "tamper", ["hash", "task", "version", "request_evidence", "plan_evidence", "approval_id"]
)
async def test_forged_or_stale_approval_rejected(database: Database, tamper: str) -> None:
    request, _ = await waiting(database)
    store = ApprovalStore(database, Settings(APP_ENV="test"))
    prompt = await store.notify(request, FakeFeishuConnector())
    value = answer(prompt)
    assert value.response
    if tamper in {"request_evidence", "plan_evidence"}:
        field = "request_evidence_id" if tamper == "request_evidence" else "plan_evidence_id"
        value = replace(value, prompt=replace(prompt, **{field: str(uuid4())}))  # type: ignore[arg-type]
    else:
        field, replacement = {
            "hash": ("action_hash", "0" * 64),
            "task": ("task_id", str(uuid4())),
            "version": ("wait_version", prompt.task.version - 1),
            "approval_id": ("approval_id", str(uuid4())),
        }[tamper]
        original_response = value.response
        assert original_response is not None
        value = replace(value, response=replace(original_response, **{field: replacement}))  # type: ignore[arg-type]
    with pytest.raises((ValueError, LookupError)):
        await store.decide(value)


async def test_no_approval_and_late_approval_cannot_enter_execution(database: Database) -> None:
    request, plan = await waiting(database)
    store = ApprovalStore(database, Settings(APP_ENV="test"))
    prompt = await store.notify(request, FakeFeishuConnector())
    with pytest.raises(ValueError):
        await TaskActivityStore(database).transition(
            TransitionRequest(request.task, TaskStatus.EXECUTING, "无审批")
        )
    await TaskActivityStore(database).transition(
        TransitionRequest(request.task, TaskStatus.ESCALATED, "超时")
    )
    with pytest.raises(ValueError, match="过期"):
        await store.decide(answer(prompt))
    assert not await store.is_approved(prompt, plan)


async def test_policy_change_invalidates_approval(database: Database) -> None:
    request, plan = await waiting(database)
    store = ApprovalStore(database, Settings(APP_ENV="test"))
    prompt = await store.notify(request, FakeFeishuConnector())
    await store.decide(answer(prompt))
    blocked = ApprovalStore(
        database,
        Settings(
            APP_ENV="test",
            POLICY_CONFIG={
                "rules": [
                    {
                        "id": "new-deny",
                        "risk_levels": ["L3"],
                        "decision": "deny",
                        "reason": "规则变化",
                    }
                ]
            },
        ),
    )
    assert not await blocked.is_approved(prompt, plan)


@pytest.mark.parametrize("phase", ["notify", "decide"])
async def test_audit_failure_is_atomic_and_sent_card_deduplicates(
    database: Database, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    request, _ = await waiting(database)
    connector, store = FakeFeishuConnector(), ApprovalStore(database, Settings(APP_ENV="test"))
    prompt = await store.notify(request, connector) if phase == "decide" else None
    original = LedgerService.append_audit

    async def fail(self: LedgerService, **kwargs: object) -> object:
        raise RuntimeError("事务失败")

    monkeypatch.setattr(LedgerService, "append_audit", fail)
    with pytest.raises(RuntimeError):
        if prompt:
            await store.decide(answer(prompt))
        else:
            await store.notify(request, connector)
    monkeypatch.setattr(LedgerService, "append_audit", original)
    async with database.session() as session:
        evidence = await LedgerService(session).evidence_for_task(UUID(request.task.task_id))
        assert not any(
            e.source_tool == ("approval.decision" if prompt else "approval.request")
            for e in evidence
        )
    prompt = prompt or await store.notify(request, connector)
    await store.decide(answer(prompt))
    assert len(connector.sent_messages) == 1


async def wait_prompt(handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress]) -> ApprovalPrompt:
    async with asyncio.timeout(30):
        while True:
            progress = await handle.query(AITaskWorkflow.progress)
            if progress.approval_prompt:
                return progress.approval_prompt
            if progress.task and progress.task.status is TaskStatus.ESCALATED:
                raise AssertionError("审批提前失败")
            await asyncio.sleep(0.02)


@local_temporal
@pytest.mark.parametrize("decision", ["approved", "rejected", "expired"])
async def test_temporal_decision_restart_no_writes_and_replay(
    database: Database, decision: str
) -> None:
    await seed(database)
    settings, connector = runtime_settings(), FakeFeishuConnector()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    value = WorkflowInput(
        task.task_id,
        investigation_json=SPEC.model_dump_json(),
        human_timeout_seconds=0.1 if decision == "expired" else 60,
    )
    async with create_worker(
        client, database, settings, feishu_connector=connector, max_cached_workflows=0
    ):
        handle = await start_task_workflow(
            client, value, task_queue=settings.temporal_config.task_queue
        )
        prompt = await wait_prompt(handle)
        if decision != "expired":
            await handle.signal(
                AITaskWorkflow.human_response,
                HumanResponse(prompt.task.status, prompt.task.version, True),
            )
            wrong = replace(signal_value(prompt), action_hash="0" * 64)
            await handle.signal(AITaskWorkflow.approve_actions, wrong)
            assert (await handle.query(AITaskWorkflow.progress)).task == prompt.task
    if decision != "expired":
        await handle.signal(AITaskWorkflow.approve_actions, signal_value(prompt, decision))
        await handle.signal(
            AITaskWorkflow.approve_actions,
            signal_value(prompt, "rejected" if decision == "approved" else "approved"),
        )
    async with create_worker(
        client, database, settings, feishu_connector=connector, max_cached_workflows=0
    ):
        result = await asyncio.wait_for(handle.result(), 30)
    assert result.task and result.task.status is (
        TaskStatus.EXECUTING if decision == "approved" else TaskStatus.ESCALATED
    )
    assert result.approval_result and result.approval_result.decision == decision
    assert len(connector.sent_messages) == 1
    async with database.session() as session:
        ledger = LedgerService(session)
        history = await TaskService(session).history(UUID(task.task_id))
        assert [(h.to_status, h.sequence) for h in history] == [
            (s.status, s.version) for s in result.history
        ]
        assert not any(
            a.event_type is AuditEventType.EXECUTION
            for a in await ledger.audits_for_task(UUID(task.task_id))
        )
        if decision != "approved":
            assert TaskStatus.EXECUTING not in [h.to_status for h in history]
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


@local_temporal
async def test_temporal_decision_commit_response_loss_reuses_approval(database: Database) -> None:
    await seed(database)
    settings, connector = runtime_settings(), FakeFeishuConnector()
    client = await Client.connect(settings.temporal_config.address)
    approval = ApprovalActivities(database, settings, connector=connector)
    calls = 0

    @activity.defn(name="approval.decide")
    async def lose_response(request: ApprovalDecisionRequest) -> ApprovalResult:
        nonlocal calls
        result = await approval.decide(request)
        calls += 1
        if calls == 1:
            raise ApplicationError("批准提交后丢响应")
        return result

    # 使用相同官方 Worker 注册，仅替换一个 Activity 以注入已提交后丢响应。
    from temporalio.worker import Worker

    from app.agent.activities import AgentActivities
    from app.agent.reviewer.activities import ReviewerActivities
    from app.runbooks.activities import RunbookActivities
    from app.tasks.activities import TaskActivities

    tasks, agent = TaskActivities(TaskActivityStore(database)), AgentActivities(database, settings)
    async with Worker(
        client,
        task_queue=settings.temporal_config.task_queue,
        workflows=[AITaskWorkflow],
        activities=[
            SafetyActivities(database, settings).check,
            SafetyActivities(database, settings).notify,
            tasks.load,
            tasks.transition,
            tasks.placeholder_stage,
            agent.investigate,
            agent.validate_conclusion,
            ReviewerActivities(database, settings).review,
            RunbookActivities(database, settings).match,
            PlanningActivities(database, settings).plan,
            approval.notify,
            lose_response,
        ],
    ):
        task = await new_task(database)
        handle = await start_task_workflow(
            client,
            WorkflowInput(task.task_id, investigation_json=SPEC.model_dump_json()),
            task_queue=settings.temporal_config.task_queue,
        )
        await handle.signal(AITaskWorkflow.approve_actions, signal_value(await wait_prompt(handle)))
        result = await asyncio.wait_for(handle.result(), 30)
    assert calls == 2 and result.task and result.task.status is TaskStatus.EXECUTING
    async with database.session() as session:
        evidence = await LedgerService(session).evidence_for_task(UUID(task.task_id))
        assert len([e for e in evidence if e.source_tool == "approval.decision"]) == 1
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
