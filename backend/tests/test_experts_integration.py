"""专家真实 Ledger/Dispatcher 与本机 Temporal 的隔离验收。"""

import asyncio
import json
import os
from uuid import UUID, uuid4

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.agent.activities import AgentActivities
from app.agent.client import LLMClient
from app.agent.experts.demo_scenario import consultation_response
from app.agent.experts.models import TOOL_ALLOWLISTS, ExpertAdvice, ExpertKind, ExpertRequest
from app.agent.experts.scenario import configured_expert_llm
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import AgentConclusion, InvestigationResult
from app.agent.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall
from app.agent.reviewer.activities import ReviewerActivities
from app.agent.workflow_models import InvestigationRequest
from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.runbooks.activities import RunbookActivities
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.models import AITask
from app.tasks.planning.activities import PlanningActivities
from app.tasks.safety.activities import SafetyActivities
from app.tasks.states import TaskStatus
from app.tasks.worker import start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import WorkflowInput
from app.tools.experts import DispatcherExpertIO, ExpertRuntime
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.runtime import investigation_registry
from tests.test_main_agent import SPEC
from tests.test_main_agent_integration import (
    accept,
    investigating,
    new_task,
    runtime_settings,
)
from tests.test_main_agent_integration import (
    database as database,
)
from tests.test_main_agent_integration import (
    migrated_schema as migrated_schema,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-experts.ps1 使用本机临时库"
    ),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需要本机 Temporal"
)


def main_factory(request: ChatRequest) -> LLMClient:
    return FakeLLM([ScriptedChatStep(consultation_response)])


def outside_then_valid(kind: ExpertKind, request: ChatRequest) -> LLMClient:
    count = sum(message.role == "tool" for message in request.messages)
    assert all(tool.function.name in TOOL_ALLOWLISTS[kind] for tool in request.tools)
    if count == 0:
        return FakeLLM(
            [
                ScriptedChatStep(
                    lambda value: ChatResponse(
                        id="outside",
                        model="fake",
                        finish_reason="tool_calls",
                        message=ChatMessage(
                            role="assistant",
                            tool_calls=(
                                ToolCall(
                                    id="outside-whitelist",
                                    function=FunctionCall(
                                        name="get_recent_changes",
                                        arguments=json.dumps(
                                            {
                                                "service_name": SPEC.service_name,
                                                "end": SPEC.end.isoformat(),
                                                "lookback_seconds": 3600,
                                            }
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    )
                )
            ]
        )
    if count == 1:
        return FakeLLM(
            [
                ScriptedChatStep(
                    lambda value: ChatResponse(
                        id="allowed",
                        model="fake",
                        finish_reason="tool_calls",
                        message=ChatMessage(
                            role="assistant",
                            tool_calls=(
                                ToolCall(
                                    id="within-whitelist",
                                    function=FunctionCall(
                                        name="query_metrics",
                                        arguments=json.dumps(
                                            {
                                                "service_name": SPEC.service_name,
                                                "start": SPEC.start.isoformat(),
                                                "end": SPEC.end.isoformat(),
                                            }
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    )
                )
            ]
        )
    return configured_expert_llm(Settings(APP_ENV="test"), kind, request)


async def test_main_database_whitelist_holmes_opinions_and_final_ownership(
    database: Database,
) -> None:
    task = await investigating(database)
    agent = AgentActivities(
        database,
        Settings(APP_ENV="test"),
        llm_factory=main_factory,
        expert_llm_factory=outside_then_valid,
    )
    result = await agent.investigate(InvestigationRequest(task, SPEC.model_dump_json()))
    assert result.steps == 19
    conclusion_id = await accept(database, task, result)
    async with database.session() as session:
        ledger = LedgerService(session)
        evidence = await ledger.evidence_for_task(UUID(task.task_id))
        advice = [item for item in evidence if item.source_tool == "consult_expert"]
        assert len(advice) == 2
        for item in advice:
            saved = ExpertAdvice.model_validate_json(json.dumps(item.result_snapshot))
            assert saved.opinion.evidence_ids <= set(saved.observed_ids)
            for reference in saved.opinion.evidence_ids:
                assert (await ledger.get_evidence(reference)).task_id == UUID(task.task_id)
        assert [
            ExpertAdvice.model_validate_json(json.dumps(item.result_snapshot)).expert
            for item in advice
        ] == [ExpertKind.DATABASE, ExpertKind.HOLMESGPT]
        audits = await ledger.audits_for_task(UUID(task.task_id))
        expert_audits = [item for item in audits if item.actor == "expert:Database"]
        assert [(item.operation, item.outcome) for item in expert_audits] == [
            ("get_recent_changes", "rejected"),
            ("query_metrics", "succeeded"),
        ]
        assert expert_audits[0].details["error_code"] == "expert_tool_not_allowed"
        assert expert_audits[0].evidence_id is None
        conclusion = AgentConclusion.model_validate_json(result.conclusion_json)
        assert {item.id for item in advice} <= conclusion.evidence_ids
        final = await ledger.get_evidence(UUID(conclusion_id))
        assert final.source_tool == "agent.conclusion"
        assert "主 Agent" in conclusion.root_cause.statement


@pytest.mark.parametrize("kind", [kind for kind in ExpertKind if kind is not ExpertKind.HOLMESGPT])
async def test_six_experts_query_via_dispatcher_and_consult_replays(
    database: Database, kind: ExpertKind
) -> None:
    settings = Settings(APP_ENV="test")
    task = await investigating(database)
    async with database.session() as session:
        async with investigation_registry(settings, session) as registry:
            runtime = ExpertRuntime(session, registry, settings, UUID(task.task_id), SPEC)
            runtime.register()
            runtime.remaining_steps = 8
            dispatcher = runtime.dispatcher
            parameters = json.loads(
                ExpertRequest(
                    **SPEC.model_dump(), expert=kind, question="核查专长范围内的证据"
                ).model_dump_json()
            )
            async with session.begin():
                result = await dispatcher.dispatch(
                    task_id=UUID(task.task_id),
                    tool_name="consult_expert",
                    parameters=parameters,
                    actor="codex-main-agent",
                )
                assert result.status is DispatchStatus.SUCCEEDED
                assert result.result is not None and result.evidence_id is not None
                # 已采集不等于已入库；截止点必须在证据和成功审计写入后。
                cutoff = utc_now()
                expert_audits = [
                    item
                    for item in await runtime.ledger.audits_for_task(UUID(task.task_id))
                    if item.actor == f"expert:{kind.value}"
                ]
                assert (
                    len(expert_audits) == 1 and expert_audits[0].operation in TOOL_ALLOWLISTS[kind]
                )
                assert expert_audits[0].evidence_id is not None
            async with session.begin():
                replay = await dispatcher.dispatch(
                    task_id=UUID(task.task_id),
                    tool_name="consult_expert",
                    parameters=parameters,
                    actor="replay",
                    mode=DispatchMode.REPLAY,
                    replay_evidence_id=result.evidence_id,
                    replay_before=cutoff,
                )
                assert replay.status is DispatchStatus.REPLAYED and replay.result == result.result
                assert replay.evidence_id == result.evidence_id
                audits = await runtime.ledger.audits_for_task(UUID(task.task_id))
                assert sum(item.actor == f"expert:{kind.value}" for item in audits) == 1


@pytest.mark.parametrize("kind", [kind for kind in ExpertKind if kind is not ExpertKind.HOLMESGPT])
async def test_all_roles_cannot_recursively_consult(database: Database, kind: ExpertKind) -> None:
    task = await investigating(database)
    settings = Settings(APP_ENV="test")
    async with database.session() as session:
        async with investigation_registry(settings, session) as registry:
            runtime = ExpertRuntime(session, registry, settings, UUID(task.task_id), SPEC)
            runtime.register()
            io = DispatcherExpertIO(runtime, kind)
            assert all(tool.function.name != "consult_expert" for tool in io.definitions)
            async with session.begin():
                result = await io.call(
                    ToolCall(
                        id="recursive",
                        function=FunctionCall(
                            name="consult_expert",
                            arguments=ExpertRequest(
                                **SPEC.model_dump(),
                                expert=ExpertKind.DATABASE,
                                question="尝试递归咨询",
                            ).model_dump_json(),
                        ),
                    )
                )
                assert result.status is DispatchStatus.REJECTED
                assert result.error_code == "expert_tool_not_allowed"
                assert result.evidence_id is None
                assert len(await runtime.ledger.audits_for_task(UUID(task.task_id))) == 5


async def test_expert_query_obeys_policy_despite_allowlist(database: Database) -> None:
    task = await investigating(database)
    settings = Settings(
        APP_ENV="test",
        POLICY_CONFIG={
            "rules": [
                {
                    "id": "deny-expert-metrics",
                    "action_names": ["query_metrics"],
                    "risk_levels": ["L0"],
                    "environments": ["test"],
                    "decision": "deny",
                    "reason": "专家同样必须通过 Policy",
                }
            ]
        },
    )
    async with database.session() as session:
        async with investigation_registry(settings, session) as registry:
            runtime = ExpertRuntime(session, registry, settings, UUID(task.task_id), SPEC)
            runtime.register()
            runtime.remaining_steps = 8
            async with session.begin():
                result = await runtime.dispatcher.dispatch(
                    task_id=UUID(task.task_id),
                    tool_name="consult_expert",
                    parameters=json.loads(
                        ExpertRequest(
                            **SPEC.model_dump(),
                            expert=ExpertKind.DATABASE,
                            question="尝试被拒的指标查询",
                        ).model_dump_json()
                    ),
                    actor="codex-main-agent",
                )
                assert result.status is DispatchStatus.FAILED and result.evidence_id is None
                audits = await runtime.ledger.audits_for_task(UUID(task.task_id))
                assert any(
                    item.actor == "expert:Database"
                    and item.operation == "query_metrics"
                    and item.details["error_code"] == "policy_denied"
                    for item in audits
                )
                assert all(
                    item.source_tool != "consult_expert"
                    for item in await runtime.ledger.evidence_for_task(UUID(task.task_id))
                )


async def test_simple_scene_has_no_expert_invocation(database: Database) -> None:
    def forbidden(kind: ExpertKind, request: ChatRequest) -> LLMClient:
        raise AssertionError("简单场景不能调用专家")

    task = await investigating(database)
    result = await AgentActivities(
        database, Settings(APP_ENV="test"), expert_llm_factory=forbidden
    ).investigate(InvestigationRequest(task, SPEC.model_dump_json()))
    assert result.steps == 9
    async with database.session() as session:
        ledger = LedgerService(session)
        assert all(
            item.source_tool != "consult_expert"
            for item in await ledger.evidence_for_task(UUID(task.task_id))
        )
        assert all(
            not item.actor.startswith("expert:")
            for item in await ledger.audits_for_task(UUID(task.task_id))
        )


async def test_committed_consultation_retry_and_concurrent_attempt_reuse(
    database: Database,
) -> None:
    task = await investigating(database)
    agent = AgentActivities(database, Settings(APP_ENV="test"), llm_factory=main_factory)
    request = InvestigationRequest(task, SPEC.model_dump_json())
    first, second = await asyncio.gather(agent.investigate(request), agent.investigate(request))
    assert first == second and first.steps == 17

    def forbidden(*args: object) -> LLMClient:
        raise AssertionError("已提交的模型与专家不能再次调用")

    assert (
        await AgentActivities(
            database, Settings(APP_ENV="test"), llm_factory=forbidden, expert_llm_factory=forbidden
        ).investigate(request)
        == first
    )
    async with database.session() as session:
        ledger = LedgerService(session)
        audits = await ledger.audits_for_task(UUID(task.task_id))
        assert sum(item.operation == "consult_expert" for item in audits) == 2
        assert sum(item.actor == "expert:Database" for item in audits) == 1


@pytest.mark.parametrize(
    "mode", ["cross_task", "checkpoint", "missing", "scope", "budget", "disabled", "policy"]
)
async def test_rejected_consultations_do_not_create_advice(database: Database, mode: str) -> None:
    task = await investigating(database)
    settings = Settings(APP_ENV="test")
    params = ExpertRequest(**SPEC.model_dump(), expert=ExpertKind.DATABASE, question="核查容量")
    if mode in {"cross_task", "checkpoint", "missing"}:
        owner = await investigating(database) if mode == "cross_task" else task
        async with database.session() as session, session.begin():
            ref = await LedgerService(session).append_evidence(
                task_id=UUID(owner.task_id),
                source_tool="agent.think",
                parameters={},
                result_snapshot={"not": "a source fact"},
            )
        params = params.model_copy(
            update={
                "expert": ExpertKind.HOLMESGPT,
                "evidence_ids": (uuid4() if mode == "missing" else ref.id,),
            }
        )
    if mode == "scope":
        params = params.model_copy(update={"service_name": "different-service"})
    if mode == "disabled":
        settings = Settings(APP_ENV="test", AGENT_CONFIG={"experts_enabled": False})
    if mode == "policy":
        settings = Settings(
            APP_ENV="test",
            POLICY_CONFIG={
                "rules": [
                    {
                        "id": "deny-consult",
                        "action_names": ["consult_expert"],
                        "risk_levels": ["L0"],
                        "environments": ["test"],
                        "decision": "deny",
                        "reason": "拒绝咨询验收",
                    }
                ]
            },
        )
    async with database.session() as session:
        async with investigation_registry(settings, session) as registry:
            runtime = ExpertRuntime(session, registry, settings, UUID(task.task_id), SPEC)
            runtime.register()
            runtime.remaining_steps = 1 if mode == "budget" else 8
            async with session.begin():
                result = await runtime.dispatcher.dispatch(
                    task_id=UUID(task.task_id),
                    tool_name="consult_expert",
                    parameters=json.loads(params.model_dump_json()),
                    actor="codex-main-agent",
                )
                assert result.status in {DispatchStatus.REJECTED, DispatchStatus.FAILED}
                assert result.evidence_id is None
                assert all(
                    item.source_tool != "consult_expert"
                    for item in await runtime.ledger.evidence_for_task(UUID(task.task_id))
                )


async def test_failed_observe_rolls_back_expert_fact_opinion_and_audits(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await investigating(database)
    original = LedgerService.append_evidence

    async def fail(self: LedgerService, **arguments: object) -> Evidence:
        if arguments["source_tool"] == "agent.observe":
            snapshot = arguments["result_snapshot"]
            if (
                isinstance(snapshot, dict)
                and isinstance(snapshot.get("result"), dict)
                and "expert" in snapshot["result"]
            ):
                raise RuntimeError("模拟咨询已执行但观察写入失败")
        return await original(self, **arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(ApplicationError):
        await AgentActivities(
            database, Settings(APP_ENV="test"), llm_factory=main_factory
        ).investigate(InvestigationRequest(task, SPEC.model_dump_json()))
    async with database.session() as session:
        ledger = LedgerService(session)
        assert all(
            item.source_tool != "consult_expert"
            for item in await ledger.evidence_for_task(UUID(task.task_id))
        )
        assert all(
            not item.actor.startswith("expert:")
            for item in await ledger.audits_for_task(UUID(task.task_id))
        )


@local_temporal
async def test_temporal_experts_commit_response_loss_restart_and_replay(database: Database) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    calls = 0
    committed = asyncio.Event()
    agent = AgentActivities(database, settings, llm_factory=main_factory)
    tasks = TaskActivities(TaskActivityStore(database))

    @activity.defn(name="agent.investigate")
    async def lost_response(request: InvestigationRequest) -> InvestigationResult:
        nonlocal calls
        result = await agent.investigate(request)
        calls += 1
        if calls == 1:
            committed.set()
            raise ApplicationError("模拟专家结果提交后响应丢失")
        return result

    def worker() -> Worker:
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
                lost_response,
                agent.validate_conclusion,
                ReviewerActivities(database, settings).review,
                PlanningActivities(database, settings).plan,
                RunbookActivities(database, settings).match,
            ],
            max_cached_workflows=0,
        )

    async with worker():
        handle = await start_task_workflow(
            client,
            WorkflowInput(
                task.task_id, investigation_json=SPEC.model_dump_json(), human_timeout_seconds=0.1
            ),
            task_queue=settings.temporal_config.task_queue,
        )
        await asyncio.wait_for(committed.wait(), 30)
    async with worker():
        progress = await asyncio.wait_for(handle.result(), 30)
    assert calls == 2 and progress.conclusion_evidence_id is not None
    assert progress.task and progress.task.status is TaskStatus.ESCALATED
    assert TaskStatus.EXECUTING not in [item.status for item in progress.history]
    async with database.session() as session:
        ledger = LedgerService(session)
        audits = await ledger.audits_for_task(UUID(task.task_id))
        assert sum(item.operation == "consult_expert" for item in audits) == 2
        assert sum(item.actor == "expert:Database" for item in audits) == 1
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())


@local_temporal
async def test_temporal_expert_global_budget_exhaustion_escalates(database: Database) -> None:
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_config.address)
    task = await new_task(database)
    agent = AgentActivities(database, settings, llm_factory=main_factory)
    tasks = TaskActivities(TaskActivityStore(database))
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
            PlanningActivities(database, settings).plan,
            RunbookActivities(database, settings).match,
        ],
    ):
        handle = await start_task_workflow(
            client,
            WorkflowInput(
                task.task_id,
                investigation_json=SPEC.model_copy(update={"max_steps": 12}).model_dump_json(),
            ),
            task_queue=settings.temporal_config.task_queue,
        )
        progress = await asyncio.wait_for(handle.result(), 30)
    assert progress.task and progress.task.status is TaskStatus.ESCALATED
    assert progress.conclusion_evidence_id is None
    async with database.session() as session:
        assert (await session.get(AITask, UUID(task.task_id))).status is TaskStatus.ESCALATED  # type: ignore[union-attr]
    await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
