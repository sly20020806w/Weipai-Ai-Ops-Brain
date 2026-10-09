"""独立临时库内注入人工劳动，演示阈值、原始引用、去重和 Temporal 派发。"""

import asyncio
import json
import os
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.learning.automation.models import AutomationInput, AutomationResult, ManualOperation
from app.learning.automation.service import AutomationService
from app.learning.automation.workflow import AutomationDiscoveryWorkflow
from app.ledger.service import LedgerService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.triggers.activities import EventActivities
from app.triggers.schemas import NormalizedEvent
from app.triggers.service import EventService


async def seed_manual(database: Database, service: str, count: int) -> list[str]:
    ids = []
    for index in range(count):
        async with database.session() as session, session.begin():
            receipt = (
                await EventService(session).accept(
                    [
                        NormalizedEvent(
                            origin="manual",
                            source=TaskSource.HUMAN,
                            external_id=f"manual-demo:{uuid4()}",
                            service_name=service,
                            title="人工检查支付连接池",
                            occurred_at=utc_now() - timedelta(minutes=10 - index),
                        )
                    ]
                )
            )[0]
            evidence_id = await AutomationService(session).record_manual(
                ManualOperation(
                    task_id=UUID(receipt.task_id),
                    record_key=f"pool-check-{index}",
                    service_name=service,
                    operation="check-payment-connection-pool",
                    actor="demo-operator",
                    source_reference=f"fake://manual/{receipt.task_id}",
                    occurred_at=utc_now() - timedelta(minutes=5),
                )
            )
            ids.append(str(evidence_id))
    return ids


async def run_demo(url: URL) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("自动化演示只允许本机隔离临时库与 Temporal")
    database = Database(url)
    queue = f"automation-demo-{uuid4().hex}"
    settings = Settings(APP_ENV="test", AUTOMATION_CONFIG={}, TEMPORAL_CONFIG={"task_queue": queue})
    client = await Client.connect(
        address,
        namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default"),
    )
    task_ids: list[str] = []
    try:
        first_four = await seed_manual(database, "payment-service", 4)
        async with database.session() as session, session.begin():
            below = await AutomationService(session).scan(
                settings.automation_config, utc_now().isoformat()
            )
        assert below.receipts == []
        fifth = await seed_manual(database, "payment-service", 1)
        async with create_worker(client, database, settings):
            handle = await client.start_workflow(
                AutomationDiscoveryWorkflow.run,
                AutomationInput(),
                id=f"automation-demo-{uuid4().hex}",
                task_queue=queue,
            )
            result: AutomationResult = await asyncio.wait_for(handle.result(), timeout=60)
            task_ids = [r.task_id for r in result.receipts]
            assert len(task_ids) == 1
            async with database.session() as session:
                evidence = await LedgerService(session).get_evidence(UUID(result.evidence_ids[0]))
                snapshot = evidence.result_snapshot
                assert isinstance(snapshot, dict)
                repetition = snapshot["repetition"]
                assert isinstance(repetition, dict) and isinstance(repetition["records"], list)
                references = repetition["records"]
                assert {r["evidence_id"] for r in references if isinstance(r, dict)} == set(
                    first_four + fifth
                )
            workflow_handle = client.get_workflow_handle_for(
                AITaskWorkflow.run, f"ai-task-{task_ids[0]}"
            )
            for _ in range(200):
                progress = await workflow_handle.query(AITaskWorkflow.progress)
                if progress.task and progress.task.status is TaskStatus.WAITING_INFORMATION:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("建议任务未进入统一引擎的等待状态")
            async with database.session() as session, session.begin():
                repeated = await AutomationService(session).scan(
                    settings.automation_config, utc_now().isoformat()
                )
            assert repeated.receipts[0].duplicate and repeated.evidence_ids == result.evidence_ids
            await EventActivities(database, settings, client).start_task(repeated.receipts[0])
            await Replayer(workflows=[AutomationDiscoveryWorkflow]).replay_workflow(
                await handle.fetch_history()
            )
            print(
                json.dumps(
                    {
                        "四条记录时建议任务数": len(below.receipts),
                        "五条记录时建议任务数": len(result.receipts),
                        "建议 Evidence ID": result.evidence_ids[0],
                        "原始记录引用": references,
                        "建议内容": snapshot["conclusion"],
                        "建议任务 Workflow ID": f"ai-task-{task_ids[0]}",
                        "任务状态": progress.task.status.value,
                        "重复扫描新增任务数": sum(not r.duplicate for r in repeated.receipts),
                        "Temporal 历史回放": "通过",
                        "实际运维动作数": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
    finally:
        for task_id in task_ids:
            await client.get_workflow_handle(f"ai-task-{task_id}").terminate("隔离演示结束")
        await database.dispose()
    print("Step 37 Automation Discovery Fake 演示全部通过", flush=True)
