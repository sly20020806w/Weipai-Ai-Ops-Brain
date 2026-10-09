"""本机 Fake 演示：看卡片、自行审批、精确读回批准及篡改失效。"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Replayer

from app.agent.investigation import InvestigationSpec
from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.db.session import Database
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import TimelineRequest
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.models import DiscoveryRequest
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.approval.service import ApprovalStore
from app.tasks.planning.models import ActionPlan
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import ApprovalResponse, WorkflowInput


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    if url.host != "127.0.0.1" or not (url.database or "").startswith("weipai_db_test_"):
        raise ValueError("审批演示只允许本机临时库")
    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"approval-demo-{uuid4().hex}",
        },
    )
    client = await Client.connect(settings.temporal_config.address)
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    spec = InvestigationSpec(
        service_name="payment-service",
        title="支付回滚审批验收",
        start=end - timedelta(hours=1),
        end=end,
    )
    handles = []
    try:
        await DiscoveryActivities(database, settings).refresh(
            DiscoveryRequest(end.isoformat(), 3600)
        )
        await TimelineActivities(database, settings).collect(
            TimelineRequest(spec.service_name, spec.start.isoformat(), spec.end.isoformat())
        )
        for decision in ("approved", "rejected", "expired"):
            connector = FakeFeishuConnector()
            async with database.session() as session, session.begin():
                task = await TaskService(session).create(
                    source=TaskSource.ALERT, title=spec.title, reason="Step 30 Fake 演示"
                )
            async with create_worker(
                client, database, settings, feishu_connector=connector, max_cached_workflows=0
            ):
                handle = await start_task_workflow(
                    client,
                    WorkflowInput(
                        str(task.id),
                        investigation_json=spec.model_dump_json(),
                        human_timeout_seconds=2 if decision == "expired" else 3600,
                    ),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(handle)
                async with asyncio.timeout(40):
                    while True:
                        progress = await handle.query(AITaskWorkflow.progress)
                        if progress.approval_prompt:
                            break
                        if progress.task and progress.task.status is TaskStatus.ESCALATED:
                            raise AssertionError("审批提前失败")
                        await asyncio.sleep(0.05)
                prompt = progress.approval_prompt
                assert prompt and progress.action_plan_json
                plan = ActionPlan.model_validate_json(progress.action_plan_json)
                print(f"\nWorkflow ID：{handle.id}", flush=True)
                card = connector.get_sent(UUID(prompt.approval_id)).notification
                print(f"Fake 飞书审批卡片：\n{card.card.markdown}", flush=True)  # type: ignore[union-attr]
                if decision == "approved" and interactive:
                    text = await asyncio.to_thread(input, "请输入 批准 或 拒绝：")
                    if text.strip() not in {"批准", "拒绝"}:
                        raise ValueError("只接受明确的 批准 或 拒绝")
                    decision = "approved" if text.strip() == "批准" else "rejected"
                if decision != "expired":
                    await handle.signal(
                        AITaskWorkflow.approve_actions,
                        ApprovalResponse(
                            prompt.task.task_id,
                            prompt.approval_id,
                            prompt.task.version,
                            prompt.action_hash,
                            decision,
                            "local-owner",
                        ),
                    )
                result = await asyncio.wait_for(handle.result(), 40)
                assert (
                    result.task
                    and result.approval_result
                    and result.approval_result.decision == decision
                )
                expected = TaskStatus.EXECUTING if decision == "approved" else TaskStatus.ESCALATED
                assert result.task.status is expected
                store = ApprovalStore(database, settings)
                assert await store.is_approved(prompt, plan) == (decision == "approved")
                changed = plan.model_dump(mode="json")
                changed["actions"][0]["action"]["parameters"]["to_version"] = "v2.3.5"
                assert not await store.is_approved(
                    prompt, ActionPlan.model_validate_json(json.dumps(changed))
                )
                async with database.session() as session:
                    ledger = LedgerService(session)
                    evidence = await ledger.get_evidence(UUID(result.approval_result.evidence_id))
                    assert isinstance(evidence.result_snapshot, dict)
                    assert evidence.result_snapshot["action_hash"] == prompt.action_hash
                    audits = await ledger.audits_for_task(task.id)
                    assert any(
                        a.event_type is AuditEventType.APPROVAL
                        and a.evidence_id == evidence.id
                        and a.outcome == decision
                        for a in audits
                    )
                    assert not any(a.event_type is AuditEventType.EXECUTION for a in audits)
                print(f"审批决定：{decision} → {result.task.status.value}", flush=True)
                print(
                    f"审批 Evidence ID：{evidence.id}\n操作人：{evidence.result_snapshot['actor']}",
                    flush=True,
                )
                print("动作参数改为 v2.3.5 后原审批失效：通过。实际执行次数：0。", flush=True)
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
        print("\nStep 30 审批流 Fake 演示全部通过", flush=True)
        print("批准仅进入 EXECUTING 等待 Step 32；拒绝/超时进入 ESCALATED。", flush=True)
    finally:
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 30 演示清理")
        await database.dispose()
