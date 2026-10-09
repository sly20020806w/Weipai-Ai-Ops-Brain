"""Step 49 浏览器所需的真实等待 Workflow；全部仅使用本机隔离库与 Fake。"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle

from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.learning.demo import seed_incident
from app.ledger.service import LedgerService
from app.tasks.console_queries import ConsoleQueries
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanQuestion, WorkflowInput, WorkflowProgress
from app.triggers.schemas import NormalizedEvent
from app.triggers.service import EventService


class ApprovalPageScenario:
    def __init__(self, database: Database, client: Client, settings: Settings) -> None:
        self.database, self.client, self.settings = database, client, settings
        self.handles: list[WorkflowHandle[Any, Any]] = []

    async def ready(self, handle: WorkflowHandle[AITaskWorkflow, WorkflowProgress]) -> None:
        async with asyncio.timeout(60):
            while True:
                progress = await handle.query(AITaskWorkflow.progress)
                if progress.approval_prompt or progress.human_prompt:
                    return
                if progress.task and progress.task.status is TaskStatus.ESCALATED:
                    raise RuntimeError("浏览器样例提前转人工")
                await asyncio.sleep(0.05)

    async def seed(self) -> dict[str, str]:
        identities: dict[str, str] = {}
        for kind in ("APPROVAL", "REJECT"):
            task, spec = await seed_incident(self.database, self.settings)
            handle = await start_task_workflow(
                self.client,
                WorkflowInput(
                    task.task_id,
                    investigation_json=spec.model_dump_json(),
                    # 本步验收审批信号与 EXECUTING 展示；保留既有 Workflow 授权交接分支。
                    # 不执行任何运维动作，不替换生产 Activity 或任务状态机。
                    execution_enabled=False,
                    postmortem_enabled=False,
                    human_timeout_seconds=3600,
                ),
                task_queue=self.settings.temporal_config.task_queue,
            )
            self.handles.append(handle)
            await self.ready(handle)
            identities[f"WEIPAI_FRONTEND_{kind}_ID"] = task.task_id
        for kind, status, question in (
            ("JUDGMENT", TaskStatus.NEED_HUMAN_JUDGMENT, "支付与其他业务的保障优先级如何取舍？"),
            ("INFORMATION", TaskStatus.WAITING_INFORMATION, "请补充机器无法获取的活动结束信息。"),
            ("TAKEOVER", TaskStatus.NEED_HUMAN_JUDGMENT, "需要你接管的业务处理示例"),
        ):
            async with self.database.session() as session, session.begin():
                event = (
                    await EventService(session).accept(
                        [
                            NormalizedEvent(
                                origin="manual",
                                source=TaskSource.HUMAN,
                                external_id=f"approval-pages-{uuid4().hex}",
                                service_name="payment-service",
                                title=f"浏览器人工处理：{kind}",
                                occurred_at=utc_now(),
                            )
                        ]
                    )
                )[0]
            handle = await start_task_workflow(
                self.client,
                WorkflowInput(
                    event.task_id,
                    human_questions=[HumanQuestion(status, question)],
                    human_timeout_seconds=3600,
                    postmortem_enabled=False,
                ),
                task_queue=self.settings.temporal_config.task_queue,
            )
            self.handles.append(handle)
            await self.ready(handle)
            identities[f"WEIPAI_FRONTEND_{kind}_ID"] = event.task_id
        return identities

    async def verify(self, identities: dict[str, str]) -> None:
        expected = {
            "APPROVAL": TaskStatus.EXECUTING,
            "REJECT": TaskStatus.ESCALATED,
            "JUDGMENT": TaskStatus.CLOSED,
            "INFORMATION": TaskStatus.CLOSED,
            "TAKEOVER": TaskStatus.ESCALATED,
        }
        for kind, status in expected.items():
            task_id = identities[f"WEIPAI_FRONTEND_{kind}_ID"]
            handle = self.client.get_workflow_handle(f"ai-task-{task_id}")
            if kind != "TAKEOVER":
                await asyncio.wait_for(handle.result(), 60)
            else:
                async with asyncio.timeout(30):
                    while (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                        await asyncio.sleep(0.05)
                assert (await handle.describe()).status is WorkflowExecutionStatus.CANCELED
            async with self.database.session() as session:
                queries = ConsoleQueries(session)
                assert (await queries.task(UUID(task_id))).status is status
                evidence = (await queries.evidence(100, 0, UUID(task_id))).items
                assert not any(record.source_tool == "execute_action" for record in evidence)
                audits = await LedgerService(session).audits_for_task(UUID(task_id))
                operation = (
                    "approval.decide"
                    if kind in {"APPROVAL", "REJECT"}
                    else "human.takeover"
                    if kind == "TAKEOVER"
                    else "human.answer"
                )
                recorded = [row for row in audits if row.operation == operation]
                assert len(recorded) == 1
                assert recorded[0].actor == "local-browser-owner"
                assert recorded[0].occurred_at.utcoffset() is not None
        print(
            "真实 Workflow 验证通过：批准 EXECUTING、拒绝/接管 ESCALATED、"
            "两类回答 CLOSED；运维执行 0。",
            flush=True,
        )


@asynccontextmanager
async def approval_scenario(url: URL) -> AsyncIterator[ApprovalPageScenario]:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("审批页面演示只允许本机隔离 PostgreSQL/Temporal")
    settings = Settings(
        APP_ENV="test",
        CONNECTOR_MODE="fake",
        LLM_MODE="fake",
        TEMPORAL_CONFIG={"address": address, "task_queue": f"approval-pages-{uuid4().hex}"},
    )
    database = Database(url)
    client = await Client.connect(address)
    scenario = ApprovalPageScenario(database, client, settings)
    try:
        async with create_worker(client, database, settings):
            yield scenario
    finally:
        for handle in scenario.handles:
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate("Step 49 临时浏览器样例清理")
        await database.dispose()
