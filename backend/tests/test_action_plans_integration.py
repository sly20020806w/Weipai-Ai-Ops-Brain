"""本机 PostgreSQL/Temporal：计划持久化、拒绝、恢复与 Replay。"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities
from app.agent.client import LLMClient
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.models import ChatMessage, ChatRequest
from app.agent.reviewer.activities import ReviewerActivities
from app.config import Settings
from app.db.session import Database
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.approval.activities import ApprovalActivities
from app.tasks.planning.activities import PlanningActivities
from app.tasks.planning.models import ActionPlan, PlanningRequest, PlanningResult
from app.tasks.planning.scenario import payment_plan_response
from app.tasks.safety.activities import SafetyActivities
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus
from app.tasks.worker import start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanResponse, TransitionRequest, WorkflowInput
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import evidence_counts, new_task, runtime_settings
from tests.test_reviewer_integration import database, migrated_schema, prepared
from tests.test_workflow_integration import wait_at

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="执行 check-action-plans.ps1 使用本机隔离依赖",
    ),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需本机 Temporal"
)
__all__ = ["database", "migrated_schema"]


async def planning(database: Database) -> PlanningRequest:
    rca = await prepared(database)
    result = await ReviewerActivities(database, Settings(APP_ENV="test")).review(rca)
    snapshot = await TaskActivityStore(database).transition(
        TransitionRequest(rca.task, TaskStatus.PLANNING, "Step 28 验收")
    )
    return PlanningRequest(snapshot, rca.spec_json, rca.conclusion_evidence_id, result.evidence_id)


async def test_persistent_plan_policy_and_real_evidence(database: Database) -> None:
    request = await planning(database)
    result = await PlanningActivities(database, Settings(APP_ENV="test")).plan(request)
    plan = ActionPlan.model_validate_json(result.plan_json)
    assert plan.actions[0].policy.decision.value == "need_approval"
    assert plan.actions[0].action.risk_level.value == "L3"
    async with database.session() as session:
        ledger = LedgerService(session)
        assert (await ledger.get_evidence(UUID(result.evidence_id))).result_snapshot == json.loads(
            result.plan_json
        )
        for reference in plan.summary.evidence_ids:
            assert (await ledger.get_evidence(reference)).task_id == plan.task_id
        assert all(
            item.operation != "execute_action"
            for item in await ledger.audits_for_task(plan.task_id)
        )


async def test_concurrent_retry_committed_plan_never_repeats_model(database: Database) -> None:
    request = await planning(database)
    calls = 0

    def factory(value: ChatRequest) -> LLMClient:
        nonlocal calls
        calls += 1
        return FakeLLM([ScriptedChatStep(payment_plan_response)])

    planner = PlanningActivities(database, Settings(APP_ENV="test"), llm_factory=factory)
    first, second = await asyncio.gather(planner.plan(request), planner.plan(request))
    assert first == second and calls == 1
    before = await evidence_counts(database, request.task.task_id)
    assert await planner.plan(request) == first and calls == 1
    assert await evidence_counts(database, request.task.task_id) == before


@pytest.mark.parametrize("mode", ["missing", "cross_task", "wrong_kind", "spec", "state", "policy"])
async def test_wrong_references_or_changed_input_rejected(database: Database, mode: str) -> None:
    request = await planning(database)
    settings = Settings(APP_ENV="test")
    await PlanningActivities(database, settings).plan(request)
    changed = request
    if mode == "missing":
        changed = replace(request, review_evidence_id=str(uuid4()))
    elif mode == "cross_task":
        other = await planning(database)
        changed = replace(request, conclusion_evidence_id=other.conclusion_evidence_id)
    elif mode == "wrong_kind":
        changed = replace(request, review_evidence_id=request.conclusion_evidence_id)
    elif mode == "spec":
        changed = replace(
            request, spec_json=SPEC.model_copy(update={"title": "篡改窗口上下文"}).model_dump_json()
        )
    elif mode == "state":
        await TaskActivityStore(database).transition(
            TransitionRequest(request.task, TaskStatus.INVESTIGATING, "计划失效")
        )
    else:
        settings = Settings(
            APP_ENV="test",
            POLICY_CONFIG={
                "rules": [
                    {
                        "id": "deny",
                        "risk_levels": ["L3"],
                        "decision": "deny",
                        "reason": "配置已变更",
                    }
                ]
            },
        )
    with pytest.raises(ApplicationError, match="被拒绝"):
        await PlanningActivities(database, settings).plan(changed)


@pytest.mark.parametrize("mode", ["rollback", "verification", "reference"])
async def test_invalid_model_plan_is_not_persisted(database: Database, mode: str) -> None:
    request = await planning(database)

    def factory(value: ChatRequest) -> LLMClient:
        def bad(value: ChatRequest):  # type: ignore[no-untyped-def]
            response = payment_plan_response(value)
            data = json.loads(response.message.content or "{}")
            if mode == "reference":
                data["actions"][0]["rationale"]["evidence_ids"] = [str(uuid4())]
            else:
                del data["actions"][0][mode]
            return response.model_copy(
                update={"message": ChatMessage(role="assistant", content=json.dumps(data))}
            )

        return FakeLLM([ScriptedChatStep(bad)])

    with pytest.raises(ApplicationError, match="被拒绝"):
        await PlanningActivities(database, Settings(APP_ENV="test"), llm_factory=factory).plan(
            request
        )
    async with database.session() as session:
        assert all(
            item.source_tool != "action_plan"
            for item in await LedgerService(session).evidence_for_task(UUID(request.task.task_id))
        )


async def test_failed_plan_commit_reuses_model_checkpoint(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await planning(database)
    original = LedgerService.append_evidence

    async def fail(self: LedgerService, **arguments: object) -> Evidence:
        if arguments["source_tool"] == "action_plan":
            raise RuntimeError("模拟计划持久化失败")
        return await original(self, **arguments)  # type: ignore[arg-type]

    planner = PlanningActivities(database, Settings(APP_ENV="test"))
    before = await evidence_counts(database, request.task.task_id)
    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(ApplicationError, match="生成失败"):
        await planner.plan(request)
    assert await evidence_counts(database, request.task.task_id) == (before[0] + 1, before[1])
    monkeypatch.setattr(LedgerService, "append_evidence", original)

    def no_model(value: ChatRequest) -> LLMClient:
        raise AssertionError("模型检查点已提交")

    result = await PlanningActivities(
        database, Settings(APP_ENV="test"), llm_factory=no_model
    ).plan(request)
    assert result.evidence_id


def worker(
    client: Client,
    database: Database,
    settings: Settings,
    plan_activity: Callable[[PlanningRequest], Awaitable[PlanningResult]],
) -> Worker:
    tasks, agent = TaskActivities(TaskActivityStore(database)), AgentActivities(database, settings)
    return Worker(
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
            plan_activity,
            ApprovalActivities(database, settings).notify,
            ApprovalActivities(database, settings).decide,
        ],
        max_cached_workflows=0,
    )


@local_temporal
@pytest.mark.parametrize("decision", ["need_approval", "allow", "deny"])
async def test_temporal_policy_branch_no_execution_signal_and_replay(
    database: Database, decision: str
) -> None:
    settings = runtime_settings()
    if decision != "need_approval":
        settings = settings.model_copy(
            update={
                "policy_config": Settings(
                    APP_ENV="test",
                    POLICY_CONFIG={
                        "rules": [
                            {
                                "id": "plan-rule",
                                "action_names": ["rollback_prod"],
                                "risk_levels": ["L3"],
                                "decision": decision,
                                "reason": "Step 28 分支验收",
                            }
                        ]
                    },
                ).policy_config
            }
        )
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    handle = None
    try:
        async with worker(client, database, settings, PlanningActivities(database, settings).plan):
            handle = await start_task_workflow(
                client,
                WorkflowInput(task.task_id, investigation_json=SPEC.model_dump_json()),
                task_queue=settings.temporal_config.task_queue,
            )
            if decision == "deny":
                progress = await asyncio.wait_for(handle.result(), 30)
            else:
                expected = (
                    TaskStatus.WAITING_APPROVAL
                    if decision == "need_approval"
                    else TaskStatus.WAITING_INFORMATION
                )
                snapshot = await wait_at(handle, expected)
                progress = await handle.query(AITaskWorkflow.progress)
                assert progress.action_plan_json and progress.action_plan_evidence_id
                # 布尔人工信号不构成动作级审批，即使 accepted=true 也不能执行。
                await handle.signal(
                    AITaskWorkflow.human_response,
                    HumanResponse(snapshot.status, snapshot.version, True),
                )
                if decision == "need_approval":
                    from tests.test_approval_integration import signal_value, wait_prompt

                    prompt = await wait_prompt(handle)
                    assert (await handle.query(AITaskWorkflow.progress)).task == snapshot
                    await handle.signal(
                        AITaskWorkflow.approve_actions, signal_value(prompt, "rejected")
                    )
                progress = await asyncio.wait_for(handle.result(), 30)
        assert progress.task and progress.task.status is TaskStatus.ESCALATED
        assert TaskStatus.EXECUTING not in [item.status for item in progress.history]
        assert (
            progress.action_plan_json
            and ActionPlan.model_validate_json(progress.action_plan_json).decision.value == decision
        )
        async with database.session() as session:
            history = await TaskService(session).history(UUID(task.task_id))
            assert [(item.to_status, item.sequence) for item in history] == [
                (item.status, item.version) for item in progress.history
            ]
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 28 验收清理")


@local_temporal
async def test_temporal_plan_commit_response_loss_restart_timeout_replay(
    database: Database,
) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    committed = asyncio.Event()
    calls = model_calls = 0

    def factory(value: ChatRequest) -> LLMClient:
        nonlocal model_calls
        model_calls += 1
        return FakeLLM([ScriptedChatStep(payment_plan_response)])

    planner = PlanningActivities(database, settings, llm_factory=factory)

    @activity.defn(name="task.plan_actions")
    async def lose_response(request: PlanningRequest) -> PlanningResult:
        nonlocal calls
        result = await planner.plan(request)
        calls += 1
        if calls == 1:
            committed.set()
            raise ApplicationError("计划提交后丢响应")
        return result

    handle = None
    try:
        async with worker(client, database, settings, lose_response):
            handle = await start_task_workflow(
                client,
                WorkflowInput(
                    task.task_id,
                    investigation_json=SPEC.model_dump_json(),
                    human_timeout_seconds=0.1,
                ),
                task_queue=settings.temporal_config.task_queue,
            )
            await asyncio.wait_for(committed.wait(), 30)
        async with worker(client, database, settings, lose_response):
            progress = await asyncio.wait_for(handle.result(), 30)
        assert calls == 2 and model_calls == 1
        assert progress.task and progress.task.status is TaskStatus.ESCALATED
        assert await evidence_counts(database, task.task_id) == (28, 7)
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        if handle and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
            await handle.terminate("Step 28 重启验收清理")
