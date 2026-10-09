"""真实本机 PostgreSQL/Temporal 验收，源系统与 LLM 全部 Fake。"""

import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import AgentConclusion
from app.agent.models import ChatRequest, ChatResponse
from app.agent.scenario import invalid_conclusion_response, tool_response
from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.learning.evaluation.activities import ReplayActivities
from app.learning.evaluation.demo import closed_fake_incident
from app.learning.evaluation.metrics import EvaluationService, EvaluationWindow
from app.learning.evaluation.models import EvaluationLabel, ReplayReport
from app.learning.evaluation.replay import ReplayStore, ReplayUnavailable
from app.learning.evaluation.workflow import ReplayEvaluationWorkflow
from app.ledger.models import AuditRecord
from app.ledger.service import LedgerService
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.states import TaskStatus
from app.tools.registry import ToolRegistry, json_object
from app.tools.replay import replay_registry
from tests.test_reviewer_integration import database, migrated_schema

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="运行 check-replay.ps1"),
]


def settings() -> Settings:
    return Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})


def forbid_live_handlers(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    import app.learning.evaluation.replay as replay_module

    original_registry = replay_registry
    invoke = AsyncMock(side_effect=AssertionError("Replay 不可调用任何 live 实现"))

    def instrumented_registry() -> ToolRegistry:
        registry = original_registry()
        for declaration in registry.declarations():
            # 只在测试替换已构造对象中的 handler，不能替换 slots 的类级描述符。
            object.__setattr__(registry._get(declaration.name), "invoke", invoke)
        return registry

    monkeypatch.setattr(replay_module, "replay_registry", instrumented_registry)
    return invoke


async def test_closed_incident_replay_zero_handlers_and_metrics_unchanged(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    began = utc_now()
    request = await closed_fake_incident(database, settings())
    async with database.session() as session:
        before = await EvaluationService(session).report(
            EvaluationWindow(start=began, end=utc_now())
        )
        history = list(
            await session.scalars(
                select(TaskStatusHistory).where(TaskStatusHistory.task_id == request.task_id)
            )
        )
    invoke = forbid_live_handlers(monkeypatch)
    store = ReplayStore(database, settings())
    first, second = await asyncio.gather(store.run(request), store.run(request))
    assert first == second and first.status == "completed"
    assert first.baseline_hit is first.candidate_hit is True
    assert first.misjudged is False
    assert first.candidate_tool_calls == first.baseline_tool_calls == 4
    assert len(first.observed_evidence_ids) == 4
    assert first.candidate and first.candidate.evidence_ids <= set(first.observed_evidence_ids)
    invoke.assert_not_called()
    async with database.session() as session:
        task = await session.get(AITask, request.task_id)
        assert task and task.status is TaskStatus.CLOSED
        after_history = list(
            await session.scalars(
                select(TaskStatusHistory).where(TaskStatusHistory.task_id == request.task_id)
            )
        )
        assert [h.id for h in after_history] == [h.id for h in history]
        rows = await LedgerService(session).evidence_for_task(request.task_id)
        assert sum(e.source_tool == "replay.report" for e in rows) == 1
        audits = list(
            await session.scalars(
                select(AuditRecord).where(
                    AuditRecord.task_id == request.task_id,
                    AuditRecord.actor == f"replay:{request.run_id}",
                )
            )
        )
        assert len(audits) == 4 and all(a.outcome == "replayed" for a in audits)
        after = await EvaluationService(session).report(
            EvaluationWindow(start=began, end=utc_now())
        )
        assert [m.value for m in before.metrics] == [m.value for m in after.metrics]
        sample = after.samples[0]
        assert sample.execution_verified and sample.automated
        assert sample.approval_decisions == sample.verification_attempts == 1
        assert sample.verification_failures == 0 and sample.rca_correct is True


@pytest.mark.parametrize("mode", ["missing", "budget", "forged"])
async def test_missing_history_budget_and_forged_reference_do_not_fallback(
    database: Database, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = await closed_fake_incident(database, settings())
    if mode == "missing":
        # 原 RCA 前的时间不含任何 Tool 结果；仍有历史 Runbook 匹配规格。
        async with database.session() as session:
            records = await LedgerService(session).evidence_for_task(request.task_id)
            match = next(e for e in records if e.source_tool == "runbook.match")
            request = request.model_copy(update={"cutoff": match.created_at})
    elif mode == "budget":
        request = request.model_copy(update={"max_steps": 1})

    def factory(chat: ChatRequest) -> FakeLLM:
        index = sum(m.role == "tool" for m in chat.messages)
        return FakeLLM(
            [
                ScriptedChatStep(
                    (lambda r: tool_response(r, index))
                    if index < 4
                    else invalid_conclusion_response
                )
            ]
        )

    invoke = forbid_live_handlers(monkeypatch)
    result = await ReplayStore(database, settings(), llm_factory=factory).run(request)
    assert result.status == "incomplete" and result.candidate is None
    assert result.candidate_hit is result.misjudged is None
    assert (
        result.error_code
        == {
            "missing": "historical_result_unavailable",
            "budget": "step_limit",
            "forged": "invalid_conclusion",
        }[mode]
    )
    invoke.assert_not_called()


@pytest.mark.parametrize("mode", ["future", "wrong_task", "spec", "run_id"])
async def test_invalid_replay_inputs_rejected(database: Database, mode: str) -> None:
    request = await closed_fake_incident(database, settings())
    store = ReplayStore(database, settings())
    if mode == "future":
        request = request.model_copy(update={"cutoff": utc_now() + timedelta(days=1)})
    elif mode == "wrong_task":
        other = await closed_fake_incident(database, settings())
        request = request.model_copy(update={"baseline_evidence_id": other.baseline_evidence_id})
    elif mode == "spec":
        request = request.model_copy(
            update={
                "investigation": request.investigation.model_copy(
                    update={"service_name": "unrelated-service"}
                )
            }
        )
    else:
        await store.run(request)
        request = request.model_copy(update={"candidate_version": "different-version"})
    with pytest.raises(ReplayUnavailable):
        await store.run(request)


@pytest.mark.parametrize("with_existing_runbook", [False, True])
async def test_recovery_after_saved_think_and_report_commit(
    database: Database, with_existing_runbook: bool
) -> None:
    if with_existing_runbook:
        from app.runbooks.maturity_scenario import create_sample, human_review

        await human_review(database, settings(), await create_sample(database, settings()))
    request = await closed_fake_incident(database, settings())
    from app.learning.evaluation.replay import ReplayIO

    original = ReplayIO.think
    attempts = 0

    async def fail_once(self: ReplayIO, step: int, chat: ChatRequest) -> ChatResponse:
        nonlocal attempts
        response = await original(self, step, chat)
        attempts += 1
        if attempts == 1:
            raise RuntimeError("模拟 Worker 在响应持久化后停止")
        return response

    from unittest.mock import patch

    with patch.object(ReplayIO, "think", fail_once):
        with pytest.raises(RuntimeError):
            await ReplayStore(database, settings()).run(request)
    result = await ReplayStore(database, settings()).run(request)
    assert result.status == "completed"
    async with database.session() as session:
        rows = await LedgerService(session).evidence_for_task(request.task_id)
        baseline = await LedgerService(session).get_evidence(request.baseline_evidence_id)
        phase = int(str(baseline.parameters["phase_version"])) - 1
        baseline_thinks = sum(
            e.source_tool == "agent.think" and e.parameters.get("phase_version") == phase
            for e in rows
        )
        assert sum(e.source_tool == "replay.think" for e in rows) == baseline_thinks
        assert sum(e.source_tool == "replay.observe" for e in rows) == 4
    assert await ReplayStore(database, settings()).run(request) == result


async def test_label_wrong_task_rolls_back_and_unknown_is_not_correct(database: Database) -> None:
    request = await closed_fake_incident(database, settings())
    async with database.session() as session, session.begin():
        await EvaluationService(session).label(
            request.task_id,
            EvaluationLabel(evidence_ids=(request.baseline_evidence_id,)),
            actor="human-benchmark-owner",
        )
    result = await ReplayStore(database, settings()).run(request)
    assert result.baseline_hit is result.candidate_hit is None
    other = await closed_fake_incident(database, settings())
    with pytest.raises(ValueError, match="其他任务"):
        async with database.session() as session, session.begin():
            await EvaluationService(session).label(
                request.task_id,
                EvaluationLabel(evidence_ids=(other.baseline_evidence_id,)),
                actor="owner",
            )


async def test_backdated_new_snapshot_and_late_success_audit_are_excluded(
    database: Database,
) -> None:
    request = await closed_fake_incident(database, settings())
    async with database.session() as session, session.begin():
        ledger = LedgerService(session)
        records = await ledger.evidence_for_task(request.task_id)
        context = next(e for e in records if e.source_tool == "get_service_context")
        logs = next(e for e in records if e.source_tool == "query_logs")
        poison = json_object(logs.result_snapshot)
        poison["logs"] = []
        new = await ledger.append_evidence(
            task_id=request.task_id,
            source_tool=logs.source_tool,
            parameters=logs.parameters,
            result_snapshot=poison,
            collected_at=logs.collected_at,
        )
        from app.ledger.models import AuditEventType

        await ledger.append_audit(
            task_id=request.task_id,
            event_type=AuditEventType.TOOL_CALL,
            actor="codex-main-agent",
            operation=logs.source_tool,
            outcome="succeeded",
            details={"mode": "live"},
            evidence_id=new.id,
            occurred_at=logs.collected_at,
        )
    completed = await ReplayStore(database, settings()).run(request)
    assert completed.status == "completed" and new.id not in completed.observed_evidence_ids
    assert logs.id in completed.observed_evidence_ids
    cutoff_before_context_audit = request.model_copy(
        update={"run_id": uuid4(), "cutoff": context.created_at}
    )
    missing = await ReplayStore(database, settings()).run(cutoff_before_context_audit)
    assert missing.status == "incomplete" and missing.observed_evidence_ids == ()


async def test_false_root_cause_scored_after_run_and_label_audit_is_atomic(
    database: Database,
) -> None:
    request = await closed_fake_incident(database, settings())
    from app.agent.models import ChatMessage
    from app.agent.scenario import conclusion_response

    def factory(chat: ChatRequest) -> FakeLLM:
        assert str(request.baseline_evidence_id) not in chat.model_dump_json()
        index = sum(m.role == "tool" for m in chat.messages)

        def conclude(value: ChatRequest) -> ChatResponse:
            response = conclusion_response(value)
            result = AgentConclusion.model_validate_json(response.message.content or "{}")
            result = result.model_copy(
                update={
                    "root_cause": result.root_cause.model_copy(
                        update={"statement": "错误基准：网络故障"}
                    )
                }
            )
            return response.model_copy(
                update={"message": ChatMessage(role="assistant", content=result.model_dump_json())}
            )

        return FakeLLM(
            [ScriptedChatStep((lambda r: tool_response(r, index)) if index < 4 else conclude)]
        )

    result = await ReplayStore(database, settings(), llm_factory=factory).run(request)
    assert result.status == "completed" and result.candidate_hit is False and result.misjudged
    async with database.session() as session:
        labels_before = [
            e.id
            for e in await LedgerService(session).evidence_for_task(request.task_id)
            if e.source_tool == "evaluation.label"
        ]
    with pytest.raises(ValueError, match="actor"):
        async with database.session() as session, session.begin():
            await EvaluationService(session).label(
                request.task_id,
                EvaluationLabel(evidence_ids=(request.baseline_evidence_id,)),
                actor=" ",
            )
    async with database.session() as session:
        assert labels_before == [
            e.id
            for e in await LedgerService(session).evidence_for_task(request.task_id)
            if e.source_tool == "evaluation.label"
        ]


@pytest.mark.skipif(not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需要本机 Temporal")
async def test_temporal_workflow_report_and_history_replay(database: Database) -> None:
    request = await closed_fake_incident(database, settings())
    client = await Client.connect(os.environ["TEST_TEMPORAL_ADDRESS"])
    queue = f"test-evaluation-{uuid4().hex}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[ReplayEvaluationWorkflow],
        activities=[ReplayActivities(database, settings()).replay],
    ):
        handle = await client.start_workflow(
            ReplayEvaluationWorkflow.run,
            request.model_dump_json(),
            id=f"replay-{request.run_id}",
            task_queue=queue,
        )
        result = ReplayReport.model_validate_json(await asyncio.wait_for(handle.result(), 45))
        assert result.status == "completed" and result.candidate_hit is True
        await Replayer(workflows=[ReplayEvaluationWorkflow]).replay_workflow(
            await handle.fetch_history()
        )
