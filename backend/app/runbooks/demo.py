"""独立临时库和 Temporal 队列的可复验 Runbook 演示。"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer

from app.agent.client import LLMClient
from app.agent.investigation import AgentConclusion, InvestigationSpec
from app.agent.models import EmbeddingRequest
from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.embedding import embedding_client
from app.runbooks.models import Runbook
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import RunbookDraft
from app.runbooks.service import RunbookService
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import WorkflowInput
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchMode, DispatchStatus
from app.tools.registry import ToolRegistry
from app.tools.runbooks import register_runbook_tools


async def run_demo(url: URL) -> None:
    if url.host != "127.0.0.1" or not (url.database or "").startswith("weipai_db_test_"):
        raise ValueError("Runbook 演示只允许本机独立临时库")
    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"runbook-demo-{uuid4().hex}",
        },
    )
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    spec = InvestigationSpec(
        service_name="payment-service",
        title="payment-service 5xx 升高",
        start=end - timedelta(hours=1),
        end=end,
    )
    client = await Client.connect(settings.temporal_config.address)
    handles = []
    try:
        draft = payment_runbook()
        for field in RunbookDraft.model_fields:
            data = draft.model_dump(mode="json")
            del data[field]
            try:
                RunbookDraft.model_validate_json(json.dumps(data))
            except ValidationError:
                pass
            else:
                raise AssertionError(f"缺少 {field} 时保存校验未拒绝")
        print(
            f"全部 {len(RunbookDraft.model_fields)} 个必填内容字段逐一缺失时均被拒绝。", flush=True
        )
        await DiscoveryActivities(database, settings).refresh(
            DiscoveryRequest(end.isoformat(), 3600)
        )
        await TimelineActivities(database, settings).collect(
            TimelineRequest(spec.service_name, spec.start.isoformat(), spec.end.isoformat())
        )
        for mode in ("applicable", "excluded", "empty"):
            current = (
                spec.model_copy(update={"title": "payment-service 维护期间 5xx"})
                if mode == "excluded"
                else spec
            )
            async with database.session() as session, session.begin():
                await session.execute(delete(Runbook))
                if mode != "empty":
                    await RunbookService(
                        session, lambda request: embedding_client(settings, request)
                    ).create(draft)
                task = await TaskService(session).create(
                    source=TaskSource.ALERT,
                    title=current.title,
                    reason=f"Step 25 Fake 演示：{mode}",
                )
            async with create_worker(client, database, settings):
                handle = await start_task_workflow(
                    client,
                    WorkflowInput(str(task.id), investigation_json=current.model_dump_json()),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(handle)
                async with asyncio.timeout(30):
                    while True:
                        progress = await handle.query(AITaskWorkflow.progress)
                        if progress.task and progress.task.status is TaskStatus.WAITING_APPROVAL:
                            break
                        if progress.task and progress.task.status is TaskStatus.ESCALATED:
                            raise AssertionError("演示调查意外转交人工")
                        await asyncio.sleep(0.05)
                assert progress.conclusion_json and progress.conclusion_evidence_id
                async with database.session() as session:
                    ledger = LedgerService(session)
                    evidence = await ledger.evidence_for_task(task.id)
                    match = next(e for e in evidence if e.source_tool == "runbook.match")
                    snapshot = match.result_snapshot
                    assert isinstance(snapshot, dict)
                    assert bool(snapshot["runbook_json"]) == (mode == "applicable")
                    if mode == "excluded":
                        assert "排除条件" in str(snapshot["reason"])
                    observations = [e for e in evidence if e.source_tool == "agent.observe"]
                    assert [e.parameters["step"] for e in observations] == (
                        [1, 2, 3, 4] if mode == "applicable" else [2, 4, 6, 8]
                    )
                    audits = [
                        a
                        for a in await ledger.audits_for_task(task.id)
                        if a.event_type is AuditEventType.TOOL_CALL
                    ]
                    assert [a.operation for a in audits] == [
                        "search_runbooks",
                        "get_service_context",
                        "get_recent_changes",
                        "query_metrics",
                        "query_logs",
                        "query_traces",
                        "get_recent_changes",
                    ]
                    conclusion = AgentConclusion.model_validate_json(progress.conclusion_json)
                    assert conclusion.evidence_ids == {
                        UUID(str(e.result_snapshot["evidence_id"]))
                        for e in observations
                        if isinstance(e.result_snapshot, dict)
                    }
                    history = await TaskService(session).history(task.id)
                    assert [h.to_status for h in history] == [p.status for p in progress.history]
                    assert TaskStatus.EXECUTING not in [h.to_status for h in history]
                    search = next(e for e in evidence if e.source_tool == "search_runbooks")
                    print(f"\n场景 {mode}：Task={task.id}；Workflow={handle.id}", flush=True)
                    print(f"  匹配结果：{snapshot['reason']}", flush=True)
                    print(
                        f"  search_runbooks Evidence={search.id}；匹配快照 Evidence={match.id}",
                        flush=True,
                    )
                    print("  查询顺序：" + " → ".join(a.operation for a in audits), flush=True)
                    print(
                        "  状态：" + " → ".join(p.status.value for p in progress.history),
                        flush=True,
                    )
                    print(
                        f"  结论 Evidence={progress.conclusion_evidence_id}；4 个引用已验证。",
                        flush=True,
                    )

                def no_llm(request: EmbeddingRequest) -> LLMClient:
                    raise AssertionError("Replay 不得调用 embedding")

                async with database.session() as session, session.begin():
                    registry = ToolRegistry()
                    register_runbook_tools(registry, RunbookService(session, no_llm))
                    replay = await ToolDispatcher(
                        registry, create_policy_engine(settings), LedgerService(session)
                    ).dispatch(
                        task_id=task.id,
                        tool_name="search_runbooks",
                        parameters=search.parameters,
                        actor="runbook-demo-replay",
                        mode=DispatchMode.REPLAY,
                        replay_evidence_id=search.id,
                        replay_before=utc_now(),
                    )
                    assert (
                        replay.status is DispatchStatus.REPLAYED
                        and replay.result == search.result_snapshot
                    )
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
                print("  Tool 原证据回放与 Temporal 历史回放通过。", flush=True)
        print("\nStep 25 Runbook Fake 演示全部通过；处理方案保留，当前仅执行只读诊断。", flush=True)
    finally:
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 25 演示结束，清理隔离任务")
        await database.dispose()
