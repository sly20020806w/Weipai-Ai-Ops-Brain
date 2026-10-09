"""隔离 Fake 交互验收：人工问题恢复到真实调查，最终仍等待独立审批。"""

import asyncio
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
from app.knowledge.human_drafts import HumanKnowledgeDraft
from app.ledger.service import LedgerService
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanAnswer, HumanQuestion, WorkflowInput


async def run_demo(url: URL, *, interactive: bool = False) -> None:
    if url.host != "127.0.0.1" or not (url.database or "").startswith("weipai_db_test_"):
        raise ValueError("人工问答演示只允许本机临时库")
    database = Database(url)
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"human-demo-{uuid4().hex}",
        },
    )
    client = await Client.connect(settings.temporal_config.address)
    end = datetime(2026, 10, 1, 2, tzinfo=UTC)
    spec = InvestigationSpec(
        service_name="payment-service",
        title="人工问答后继续支付调查",
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
        for status, question, default in [
            (
                TaskStatus.NEED_HUMAN_JUDGMENT,
                "支付高峰期优先稳定性还是节省成本？",
                "优先稳定性，成本可稍后处理",
            ),
            (
                TaskStatus.WAITING_INFORMATION,
                "请补充支付业务的特殊限制。",
                "高峰期不能停机，变更须先评估影响",
            ),
        ]:
            connector = FakeFeishuConnector()
            async with database.session() as session, session.begin():
                task = await TaskService(session).create(
                    source=TaskSource.HUMAN, title=spec.title, reason="Step 29 Fake 演示"
                )
            async with create_worker(
                client, database, settings, feishu_connector=connector, max_cached_workflows=0
            ):
                handle = await start_task_workflow(
                    client,
                    WorkflowInput(
                        str(task.id),
                        investigation_json=spec.model_dump_json(),
                        human_questions=[HumanQuestion(status, question)],
                    ),
                    task_queue=settings.temporal_config.task_queue,
                )
                handles.append(handle)
                async with asyncio.timeout(30):
                    while True:
                        progress = await handle.query(AITaskWorkflow.progress)
                        if progress.human_prompt:
                            break
                        if progress.task and progress.task.status is TaskStatus.ESCALATED:
                            raise AssertionError("人工问答提前失败")
                        await asyncio.sleep(0.05)
                prompt = progress.human_prompt
                assert prompt and progress.task and progress.task.status is status
                assert len(connector.sent_messages) == 1
                print(f"\n任务 {task.id} → {status.value}", flush=True)
                print(f"Fake 飞书问题卡片：{question}", flush=True)
                print(f"问题 ID：{prompt.question_id}", flush=True)
                text = (
                    await asyncio.to_thread(input, "请输入你的回答：") if interactive else default
                )
                await handle.signal(
                    AITaskWorkflow.answer_question,
                    HumanAnswer(
                        prompt.question_id, status, prompt.task.version, text, "local-owner"
                    ),
                )
                async with asyncio.timeout(45):
                    while True:
                        progress = await handle.query(AITaskWorkflow.progress)
                        if progress.task and progress.task.status is TaskStatus.WAITING_APPROVAL:
                            break
                        if progress.task and progress.task.status is TaskStatus.ESCALATED:
                            raise AssertionError("回答后调查失败")
                        await asyncio.sleep(0.05)
                assert len(progress.human_answers) == 1 and progress.conclusion_evidence_id
                saved = progress.human_answers[0]
                async with database.session() as session:
                    draft = await session.get(HumanKnowledgeDraft, UUID(saved.knowledge_draft_id))
                    assert (
                        draft
                        and draft.task_id == task.id
                        and draft.answer == text
                        and draft.status == "draft"
                    )
                    evidence = await LedgerService(session).get_evidence(
                        UUID(saved.answer_evidence_id)
                    )
                    assert evidence.task_id == task.id and evidence.result_snapshot
                    history = await TaskService(session).history(task.id)
                    assert [(h.to_status, h.sequence) for h in history] == [
                        (s.status, s.version) for s in progress.history
                    ]
                print(
                    f"回答：{text}\n回答 Evidence ID：{saved.answer_evidence_id}\n"
                    f"Knowledge 草稿 ID：{saved.knowledge_draft_id}",
                    flush=True,
                )
                print(
                    f"任务恢复并完成调查、Reviewer 与 Action Plan → {progress.task.status.value}",
                    flush=True,
                )
                print(f"Workflow ID：{handle.id}", flush=True)
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
                await handle.terminate("Step 29 演示清理；未执行生产动作")
        print("\nStep 29 人工判断与补充信息 Fake 演示全部通过", flush=True)
    finally:
        for handle in handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 29 演示清理")
        await database.dispose()
