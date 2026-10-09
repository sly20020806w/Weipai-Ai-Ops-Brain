"""本机隔离演示，自动启动 Worker 并打印可复核的风险与证据。"""

import asyncio
import json
import os
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import URL
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.inspection.fake import FakeInspectionConnector, sample_facts
from app.connectors.inspection.models import InspectionFacts
from app.db.session import Database
from app.ledger.models import AuditEventType, AuditRecord
from app.tasks.inspection.activities import InspectionActivities
from app.tasks.inspection.models import InspectionReport, RiskEntry
from app.tasks.inspection.workflow import InspectionWorkflow
from app.tasks.states import TaskStatus
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.triggers.scheduling.models import PeriodicInput, PeriodicKind
from app.triggers.scheduling.workflow import PeriodicTriggerWorkflow


async def case(
    database: Database,
    client: Client,
    snapshot: InspectionFacts,
    feishu: FakeFeishuConnector,
    kind: PeriodicKind = "workday-inspection",
) -> InspectionReport:
    queue = f"inspection-demo-{uuid4().hex}"
    settings = Settings(
        APP_ENV="test",
        INSPECTION_CONFIG={"services": [snapshot.service_name]},
        TEMPORAL_CONFIG={"task_queue": queue, "address": os.environ["TEST_TEMPORAL_ADDRESS"]},
    )
    activities = InspectionActivities(
        database,
        settings,
        connector_factory=lambda: FakeInspectionConnector(snapshot),
        feishu=feishu,
    )
    async with create_worker(client, database, settings, inspection_activities=activities):
        scheduled = await client.start_workflow(
            PeriodicTriggerWorkflow.run,
            PeriodicInput(kind, snapshot.service_name),
            id=f"inspection-periodic-demo-{uuid4().hex}",
            task_queue=queue,
        )
        receipt = await asyncio.wait_for(scheduled.result(), timeout=60)
        handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
        progress = await asyncio.wait_for(handle.result(), timeout=60)
        assert progress.task and progress.task.status is TaskStatus.CLOSED
        report = InspectionReport.model_validate_json(progress.conclusion_json or "{}")
        assert report.complete
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
        child = client.get_workflow_handle_for(
            InspectionWorkflow.run, f"{receipt.workflow_id}/inspection/3"
        )
        await Replayer(workflows=[InspectionWorkflow]).replay_workflow(await child.fetch_history())
        print(
            f"巡检任务 CLOSED：{receipt.workflow_id}\n"
            f"报告 Evidence: {progress.conclusion_evidence_id}",
            flush=True,
        )
        return report


async def run_demo(url: URL) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("巡检演示只允许本机临时库与 Temporal")
    database = Database(url)
    client = await Client.connect(
        address, namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default")
    )
    feishu = FakeFeishuConnector()
    try:
        report = await case(database, client, sample_facts("payment-service"), feishu)
        async with database.session() as session:
            risks = list(await session.scalars(select(RiskEntry).where(RiskEntry.active)))
        assert len(risks) == 4 and len(feishu.sent_messages) == 4
        print("异常环境风险条目：4", flush=True)
        for check in report.checks:
            if check.outcome == "abnormal":
                print(
                    f"  {check.label} / {check.resource} / {check.outcome} "
                    f"/ Evidence: {check.evidence_id}",
                    flush=True,
                )
        await case(database, client, sample_facts("payment-service"), feishu, "daily-governance")
        async with database.session() as session:
            repeated = list(await session.scalars(select(RiskEntry).where(RiskEntry.active)))
        assert len(repeated) == 4 and len(feishu.sent_messages) == 4
        before = len(feishu.sent_messages)
        healthy = await case(
            database, client, sample_facts("healthy-service", abnormal=False), feishu
        )
        await case(
            database,
            client,
            sample_facts("healthy-service", abnormal=False),
            feishu,
            "hourly-capacity",
        )
        assert all(c.outcome == "healthy" for c in healthy.checks)
        assert len(feishu.sent_messages) == before
        async with database.session() as session:
            actions = list(
                await session.scalars(
                    select(AuditRecord).where(AuditRecord.event_type == AuditEventType.EXECUTION)
                )
            )
        assert actions == []
        print(
            json.dumps(
                {
                    "巡检覆盖类别": len({c.area for c in report.checks}),
                    "治理分类": sorted({c.category for c in report.checks}),
                    "重复扫描新增风险": len(repeated) - len(risks),
                    "重复扫描新增通知": len(feishu.sent_messages) - 4,
                    "全部健康新增通知": len(feishu.sent_messages) - before,
                    "实际运维动作数": len(actions),
                    "Temporal 历史回放": "通过",
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
    finally:
        await database.dispose()
    print("Step 40 巡检与治理 Fake 演示全部通过", flush=True)
