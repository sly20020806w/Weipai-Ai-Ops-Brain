"""隔离 PostgreSQL / 本机 Temporal：原子学习、幂等、证据门禁与回放。"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import timedelta
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector
from app.connectors.observability.fake import SAMPLE_END
from app.db.base import utc_now
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.learning.activities import LearningActivities
from app.learning.demo import prepare_incident, seed_incident
from app.learning.engine import compose
from app.learning.models import IncidentReport, IncidentSearch, LearningRequest, LearningResult
from app.learning.service import IncidentService, LearningStore
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.models import Runbook
from app.runbooks.schemas import AutomationLevel, RunbookMaturity
from app.tasks.activities import TaskActivityStore
from app.tasks.models import AITask
from app.tasks.service import TaskService, TaskStateConflict
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import ApprovalResponse, TransitionRequest, WorkflowInput
from app.tools.dispatcher import ToolDispatcher
from app.tools.incidents import SearchIncidentsOutput, register_incident_tools
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.registry import ToolRegistry
from app.tools.verification_runtime import fake_verification_registry
from app.triggers.models import OpsEvent
from app.verifier.activities import VerifierActivities
from app.verifier.models import ResourceExpectation, VerificationRequest
from tests.test_approval_integration import wait_prompt
from tests.test_reviewer_integration import database, migrated_schema

__all__ = ["database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="运行 check-postmortem.ps1"),
]
local_temporal = pytest.mark.skipif(
    not os.environ.get("TEST_TEMPORAL_ADDRESS"), reason="需要本机 Temporal"
)


def settings() -> Settings:
    return Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})


async def learning(database: Database) -> LearningRequest:
    snapshot, spec, connector = await prepare_incident(database, settings())
    await connector.aclose()
    verifier = VerifierActivities(
        database,
        settings(),
        registry_factory=partial(
            fake_verification_registry,
            window_start=spec.start,
            window_end=spec.end,
        ),
    )
    result = await verifier.verify(VerificationRequest(snapshot, spec.model_dump_json()))
    assert result.task.status is TaskStatus.RESOLVED
    task = await TaskActivityStore(database).transition(
        TransitionRequest(result.task, TaskStatus.LEARNING, "Step 34 自动复盘")
    )
    return LearningRequest(task)


async def test_report_draft_event_tasks_and_all_references(database: Database) -> None:
    request = await learning(database)
    generated = await LearningStore(database, settings()).generate(request)
    report = IncidentReport.model_validate_json(generated.report_json)
    async with database.session() as session:
        ledger = LedgerService(session)
        for evidence_id in report.evidence_ids | {item.evidence_id for item in report.timeline}:
            assert (await ledger.get_evidence(evidence_id)).task_id == report.task_id
        draft = await session.get(Runbook, report.runbook_id)
        assert draft and draft.maturity == RunbookMaturity.DRAFT.value
        assert draft.automation_level == AutomationLevel.MANUAL.value
        assert draft.success_count == draft.failure_count == 0
        assert len(report.improvement_task_ids) == len(generated.improvements) == 4
        for receipt in generated.improvements:
            child = await session.get(AITask, UUID(receipt.task_id))
            event = await session.get(OpsEvent, UUID(receipt.event_id))
            assert child and child.source is TaskSource.AI and child.status is TaskStatus.NEW
            assert event and event.origin == "learning" and event.task_id == child.id
            refs = await ledger.evidence_for_task(child.id)
            assert refs[0].source_tool == "postmortem.origin" and refs[0].parameters[
                "incident_task_id"
            ] == str(report.task_id)
        found = await IncidentService(session).search(
            IncidentSearch(query="连接池", service_name="payment-service")
        )
        assert any(hit.evidence_id == UUID(generated.evidence_id) for hit in found)
        assert not await IncidentService(session).search(IncidentSearch(query="%"))
    assert "连接" in report.sections[4].conclusions[0].statement
    assert "全部八项" in report.sections[6].conclusions[0].statement


async def test_concurrent_retry_and_closed_task_reuse_one_report(database: Database) -> None:
    request = await learning(database)
    store = LearningStore(database, settings())
    first, second = await asyncio.gather(store.generate(request), store.generate(request))
    assert first == second
    await TaskActivityStore(database).transition(
        TransitionRequest(request.task, TaskStatus.CLOSED, "学习完成")
    )
    assert await store.generate(request) == first
    async with database.session() as session:
        records = await LedgerService(session).evidence_for_task(UUID(request.task.task_id))
        assert sum(e.source_tool == "postmortem" for e in records) == 1
        assert sum(e.source_tool == "postmortem.context" for e in records) == 1


@pytest.mark.parametrize("invalid", ["phase", "version"])
async def test_wrong_state_or_version_never_generates(database: Database, invalid: str) -> None:
    request = await learning(database)
    task = (
        replace(request.task, status=TaskStatus.RESOLVED)
        if invalid == "phase"
        else replace(request.task, version=request.task.version + 1)
    )
    with pytest.raises(TaskStateConflict):
        await LearningStore(database, settings()).generate(LearningRequest(task))


async def test_embedding_failure_rolls_back_report_draft_children_and_context(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.runbooks.service import RunbookService

    request = await learning(database)
    async with database.session() as session:
        before = await session.scalar(select(func.count()).select_from(AITask))
    original = RunbookService.create

    async def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("Fake embedding 保存失败")

    monkeypatch.setattr(RunbookService, "create", fail)
    with pytest.raises(RuntimeError):
        await LearningStore(database, settings()).generate(request)
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(AITask)) == before
        assert not any(
            e.source_tool.startswith("postmortem")
            for e in await LedgerService(session).evidence_for_task(UUID(request.task.task_id))
        )
    monkeypatch.setattr(RunbookService, "create", original)
    assert (await LearningStore(database, settings()).generate(request)).evidence_id


async def test_unknown_or_cross_task_report_reference_is_rejected(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.learning.service as service
    from app.agent.investigation import EvidenceClaim
    from app.learning.models import PostmortemSection

    request = await learning(database)
    original = compose

    def bad(*args: object) -> object:
        draft, runbook = original(*args)  # type: ignore[arg-type]
        claims = draft.sections[0].model_copy(
            update={"conclusions": (EvidenceClaim(statement="伪造引用", evidence_ids=(uuid4(),)),)}
        )
        assert isinstance(claims, PostmortemSection)
        return draft.model_copy(update={"sections": (claims, *draft.sections[1:])}), runbook

    monkeypatch.setattr(service, "compose", bad)
    with pytest.raises(ValueError, match="已接受"):
        await LearningStore(database, settings()).generate(request)


async def test_search_dispatcher_policy_audit_and_offline_replay(database: Database) -> None:
    request = await learning(database)
    generated = await LearningStore(database, settings()).generate(request)
    async with database.session() as session, session.begin():
        registry = ToolRegistry()
        register_incident_tools(registry, IncidentService(session))
        ledger = LedgerService(session)
        dispatcher = ToolDispatcher(registry, create_policy_engine(settings()), ledger)
        result = await dispatcher.dispatch(
            task_id=UUID(request.task.task_id),
            tool_name="search_incidents",
            parameters={"query": "payment-service"},
            actor="codex-main-agent",
        )
        assert result.status is DispatchStatus.SUCCEEDED and result.evidence_id and result.result
        output = SearchIncidentsOutput.model_validate_json(json.dumps(result.result))
        assert any(str(hit.evidence_id) == generated.evidence_id for hit in output.matches)
        registry._tools.clear()
        register_incident_tools(registry, IncidentService(session))
        tool = registry._get("search_incidents")

        async def forbidden(value: object) -> object:
            raise AssertionError("Replay 不得运行搜索")

        registry._tools["search_incidents"] = replace(tool, invoke=forbidden)  # type: ignore[arg-type]
        replay = await dispatcher.dispatch(
            task_id=UUID(request.task.task_id),
            tool_name="search_incidents",
            parameters={"query": "payment-service"},
            actor="codex-main-agent",
            mode=DispatchMode.REPLAY,
            replay_evidence_id=result.evidence_id,
            replay_before=utc_now(),
        )
        assert replay.status is DispatchStatus.REPLAYED and replay.result == result.result
        denied_settings = Settings(
            APP_ENV="test",
            POLICY_CONFIG={
                "rules": [
                    {
                        "id": "deny-incidents",
                        "risk_levels": ["L0"],
                        "decision": "deny",
                        "reason": "专项验收拦截",
                        "action_names": ["search_incidents"],
                    }
                ]
            },
        )
        denied = await ToolDispatcher(
            registry, create_policy_engine(denied_settings), ledger
        ).dispatch(
            task_id=UUID(request.task.task_id),
            tool_name="search_incidents",
            parameters={"query": "payment-service"},
            actor="codex-main-agent",
        )
        assert denied.status is DispatchStatus.REJECTED and denied.error_code == "policy_denied"


async def test_final_report_failure_rolls_back_all_learning_outputs(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await learning(database)
    async with database.session() as session:
        before_tasks = await session.scalar(select(func.count()).select_from(AITask))
        before_runbooks = await session.scalar(select(func.count()).select_from(Runbook))
    original = LedgerService.append_evidence

    async def fail(self: LedgerService, **kwargs: object) -> Evidence:
        if kwargs.get("source_tool") == "postmortem":
            raise RuntimeError("模拟末尾复盘持久化失败")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerService, "append_evidence", fail)
    with pytest.raises(RuntimeError):
        await LearningStore(database, settings()).generate(request)
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(AITask)) == before_tasks
        assert await session.scalar(select(func.count()).select_from(Runbook)) == before_runbooks
        records = await LedgerService(session).evidence_for_task(UUID(request.task.task_id))
        assert not any(e.source_tool.startswith("postmortem") for e in records)


@local_temporal
@pytest.mark.parametrize("lost_response", [False, True])
async def test_temporal_closed_child_dispatch_retry_and_replay(
    database: Database, lost_response: bool
) -> None:
    config = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"postmortem-{uuid4().hex}",
        },
    )
    snapshot, spec, connector = await prepare_incident(database, config)

    class LoseResponse(LearningActivities):
        calls = 0

        @activity.defn(name="learning.postmortem")
        async def generate(self, request: LearningRequest) -> LearningResult:
            result = await super().generate(request)
            self.calls += 1
            if lost_response and self.calls == 1:
                raise ApplicationError("学习已提交但响应丢失")
            return result

    learner = LoseResponse(database, config)
    verifier = VerifierActivities(
        database,
        config,
        registry_factory=partial(
            fake_verification_registry,
            window_start=spec.start,
            window_end=spec.end,
        ),
    )
    client = await Client.connect(config.temporal_config.address)
    child_handles = []
    try:
        async with create_worker(
            client, database, config, verifier_activities=verifier, learning_activities=learner
        ):
            handle = await start_task_workflow(
                client,
                WorkflowInput(snapshot.task_id, verification_json=spec.model_dump_json()),
                task_queue=config.temporal_config.task_queue,
            )
            progress = await asyncio.wait_for(handle.result(), 45)
            assert (
                progress.task
                and progress.task.status is TaskStatus.CLOSED
                and progress.postmortem_json
            )
            report = IncidentReport.model_validate_json(progress.postmortem_json)
            assert learner.calls == (2 if lost_response else 1)
            for task_id in report.improvement_task_ids:
                child = client.get_workflow_handle(f"ai-task-{task_id}")
                child_handles.append(child)
                assert (await child.describe()).status is WorkflowExecutionStatus.RUNNING
            async with database.session() as session:
                history = await TaskService(session).history(UUID(snapshot.task_id))
                assert [h.to_status for h in history[-3:]] == [
                    TaskStatus.RESOLVED,
                    TaskStatus.LEARNING,
                    TaskStatus.CLOSED,
                ]
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
            assert connector.execution_count == 1
    finally:
        for child in child_handles:
            if (await child.describe()).status is WorkflowExecutionStatus.RUNNING:
                await child.terminate("Step 34 验收清理")
        await connector.aclose()


@local_temporal
@pytest.mark.parametrize("configured_resources", [True, False])
async def test_primary_workflow_closes_or_requires_explicit_resource_criteria(
    database: Database,
    configured_resources: bool,
) -> None:
    resources = (
        {
            "payment-service": (
                ResourceExpectation(
                    product="rds",
                    region_id="cn-hangzhou",
                    resource_id="rm-payment",
                    healthy_status="Running",
                ),
            )
        }
        if configured_resources
        else {}
    )
    config = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        VERIFICATION_CONFIG={"resources_by_service": resources},
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"primary-learning-{uuid4().hex}",
        },
    )
    snapshot, spec = await seed_incident(database, config)
    connector = FakeKubernetesWriteConnector(clock=lambda: SAMPLE_END)
    executor = ExecutorActivities(database, config, connector=connector)
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
    children = []
    try:
        async with create_worker(
            client, database, config, verifier_activities=verifier, executor_activities=executor
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
                    "local-fake-owner",
                ),
            )
            result = await asyncio.wait_for(handle.result(), 45)
            assert result.task and result.task.status is (
                TaskStatus.CLOSED if configured_resources else TaskStatus.ESCALATED
            )
            assert connector.execution_count == 1
            if configured_resources:
                assert result.postmortem_json
                report = IncidentReport.model_validate_json(result.postmortem_json)
                children = [
                    client.get_workflow_handle(f"ai-task-{task_id}")
                    for task_id in report.improvement_task_ids
                ]
                async with database.session() as session:
                    history = await TaskService(session).history(UUID(snapshot.task_id))
                    assert [(h.to_status, h.sequence) for h in history] == [
                        (h.status, h.version) for h in result.history
                    ]
            else:
                assert result.postmortem_json is None
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        for child in children:
            if (await child.describe()).status is WorkflowExecutionStatus.RUNNING:
                await child.terminate("Step 34 主链路验收清理")
        await connector.aclose()


@local_temporal
async def test_unrecovered_incident_never_enters_learning(database: Database) -> None:
    config = Settings(
        APP_ENV="test",
        EXECUTION_CONFIG={"enabled": True},
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"unrecovered-{uuid4().hex}",
        },
    )
    snapshot, spec, connector = await prepare_incident(database, config)
    verifier = VerifierActivities(
        database,
        config,
        registry_factory=partial(
            fake_verification_registry,
            recovered=False,
            window_start=spec.start,
            window_end=spec.end,
        ),
    )
    client = await Client.connect(config.temporal_config.address)
    try:
        async with create_worker(client, database, config, verifier_activities=verifier):
            handle = await start_task_workflow(
                client,
                WorkflowInput(
                    snapshot.task_id,
                    verification_json=spec.model_dump_json(),
                ),
                task_queue=config.temporal_config.task_queue,
            )
            result = await asyncio.wait_for(handle.result(), 30)
        assert result.task and result.task.status in {
            TaskStatus.INVESTIGATING,
            TaskStatus.AUTOMATION_ABORTED,
        }
        assert result.postmortem_json is result.postmortem_evidence_id is None
        async with database.session() as session:
            records = await LedgerService(session).evidence_for_task(UUID(snapshot.task_id))
            assert not any(e.source_tool.startswith("postmortem") for e in records)
    finally:
        await connector.aclose()
