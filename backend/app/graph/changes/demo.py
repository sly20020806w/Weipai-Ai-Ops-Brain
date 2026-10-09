"""人工验收固定 Fake 历史，使用同一个 worker 入口。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from temporalio.client import Client

from app.config import Settings
from app.db.session import Database
from app.graph.changes.schemas import TimelineInput
from app.graph.changes.service import TimelineService
from app.graph.changes.workflow import ChangeTimelineWorkflow
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.service import TaskService
from app.tasks.states import TaskSource
from app.tasks.worker import validate_placeholder_settings
from app.tools import DispatchMode, DispatchStatus, ToolDispatcher
from app.tools.models import JsonObject
from app.tools.registry import ToolRegistry
from app.tools.timeline import RecentChangesOutput, register_timeline_tools


async def run_demo() -> None:
    settings = Settings()
    validate_placeholder_settings(settings)
    client = await Client.connect(
        settings.temporal_config.address, namespace=settings.temporal_config.namespace
    )
    database = Database(settings.require_database_url())
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    start = end - timedelta(hours=1)
    previous = None
    try:
        for index in (1, 2):
            handle = await client.start_workflow(
                ChangeTimelineWorkflow.run,
                TimelineInput(end=end.isoformat()),
                id=f"timeline-demo-{uuid4().hex}",
                task_queue=settings.temporal_config.task_queue,
            )
            print(f"第 {index} 次 Workflow ID：{handle.id}", flush=True)
            result = await asyncio.wait_for(handle.result(), 60)
            assert result.event_count == 8 and not result.missing_bindings
            async with database.session() as session:
                events = await TimelineService(session).recent("payment-service", start, end)
            print(
                f"采集 {result.event_count} 条变更，本次新增 {result.inserted_count} 条", flush=True
            )
            if previous is not None:
                assert previous == events and result.inserted_count == 0
                print("重复采集通过：记录 ID、发生时间和首次采集时间不变", flush=True)
            previous = events
        async with database.session() as session, session.begin():
            task = await TaskService(session).create(
                source=TaskSource.HUMAN, title="变更时间线验收（Fake）", reason="Step 19 人工验收"
            )
            ledger = LedgerService(session)
            registry = ToolRegistry()
            register_timeline_tools(registry, TimelineService(session))
            dispatcher = ToolDispatcher(registry, create_policy_engine(settings), ledger)
            parameters: JsonObject = {
                "service_name": "payment-service",
                "lookback_seconds": 3600,
                "end": end.isoformat(),
            }
            response = await dispatcher.dispatch(
                task_id=task.id,
                tool_name="get_recent_changes",
                parameters=parameters,
                actor="local-demo",
            )
            assert response.status is DispatchStatus.SUCCEEDED and response.evidence_id is not None
            assert response.result is not None
            output = RecentChangesOutput.model_validate_json(json.dumps(response.result))
            core = ("Commit", "Merge", "Build", "Image", "Sync", "Deploy")
            assert tuple(e.kind for e in output.events if e.kind in core) == core
            assert all(e.occurred_at.tzinfo is UTC for e in output.events)
            print("核心时间链路：" + " → ".join(core), flush=True)
            for event in output.events:
                print(
                    f"{event.occurred_at.isoformat()}  {event.kind}  {event.source_ref}", flush=True
                )
            assert len(await ledger.evidence_for_task(task.id)) == 1
            assert (
                len(
                    [
                        a
                        for a in await ledger.audits_for_task(task.id)
                        if a.event_type is AuditEventType.TOOL_CALL
                    ]
                )
                == 1
            )
            print(
                f"L0 查询通过：1 条 Evidence + 1 条 Tool 审计；Evidence ID：{response.evidence_id}",
                flush=True,
            )
            evidence = await ledger.get_evidence(response.evidence_id)
            replay = await dispatcher.dispatch(
                task_id=task.id,
                tool_name="get_recent_changes",
                parameters=parameters,
                actor="replay",
                mode=DispatchMode.REPLAY,
                replay_evidence_id=evidence.id,
                replay_before=evidence.collected_at + timedelta(seconds=1),
            )
            assert replay.status is DispatchStatus.REPLAYED and replay.result == response.result
            print("Replay 通过：复用原 Evidence ID 与原变更快照", flush=True)
        print("Step 19 演示通过；Temporal UI 可查看两条 ChangeTimelineWorkflow", flush=True)
    finally:
        await database.dispose()


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
