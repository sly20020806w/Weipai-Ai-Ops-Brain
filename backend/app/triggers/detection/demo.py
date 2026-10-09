"""实际本机 Schedule 演示，所有运维数据由显式 Fake 提供。"""

import asyncio
import os
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, ScheduleActionExecutionStartWorkflow

from app.config import Settings
from app.connectors.kubernetes.fake import FakeKubernetesConnector, sample_snapshot
from app.connectors.observability.detection_fake import FakeDetectionPrometheusConnector
from app.db.session import Database
from app.ledger.service import LedgerService
from app.tasks.models import AITask
from app.tasks.states import TaskStatus
from app.tasks.worker import create_worker
from app.triggers.detection.config import DetectionConfig
from app.triggers.detection.models import DetectionResult
from app.triggers.detection.schedule import ensure_detection_schedule
from app.triggers.schemas import EventReceipt


async def run_demo(url: URL) -> None:
    token = uuid4().hex
    settings = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"detection-demo-{token}",
        },
        DETECTION_CONFIG=DetectionConfig(schedule_id=f"detection-demo-{token}"),
    )
    client = await Client.connect(settings.temporal_config.address, namespace="default")
    database = Database(url)
    receipts: list[EventReceipt] = []
    healthy = False

    def kubernetes_factory(settings: Settings) -> FakeKubernetesConnector:
        snapshot = sample_snapshot()
        if healthy:
            deployment = snapshot.deployments[0]
            ready = deployment.model_copy(
                update={"status": deployment.status.model_copy(update={"ready_replicas": 3})}
            )
            snapshot = snapshot.model_copy(update={"deployments": (ready,)})
        return FakeKubernetesConnector(snapshot)

    def metrics_factory(settings: Settings) -> FakeDetectionPrometheusConnector:
        return FakeDetectionPrometheusConnector(healthy=healthy)

    created = False
    try:
        created = await ensure_detection_schedule(
            client, settings.detection_config, settings.temporal_config.task_queue
        )
        assert created
        schedule = client.get_schedule_handle(settings.detection_config.schedule_id)
        await schedule.pause(note="演示只手动触发，退出时删除")
        with (
            patch(
                "app.triggers.detection.activities.create_kubernetes_connector", kubernetes_factory
            ),
            patch("app.triggers.detection.activities.create_prometheus_connector", metrics_factory),
        ):
            async with create_worker(client, database, settings):

                async def trigger() -> DetectionResult:
                    before = (await schedule.describe()).info.num_actions
                    await schedule.trigger()
                    async with asyncio.timeout(30):
                        while (description := await schedule.describe()).info.num_actions <= before:
                            await asyncio.sleep(0.05)
                    action = description.info.recent_actions[-1].action
                    assert isinstance(action, ScheduleActionExecutionStartWorkflow)
                    return await asyncio.wait_for(
                        client.get_workflow_handle(
                            action.workflow_id, result_type=DetectionResult
                        ).result(),
                        30,
                    )

                first = await trigger()
                receipts.extend(first.receipts)
                assert len(first.receipts) == 5 and all(not r.duplicate for r in first.receipts)
                async with asyncio.timeout(30):
                    while True:
                        async with database.session() as session:
                            tasks = [
                                await session.get(AITask, UUID(receipt.task_id))
                                for receipt in receipts
                            ]
                        if all(
                            task and task.status is TaskStatus.WAITING_INFORMATION for task in tasks
                        ):
                            break
                        await asyncio.sleep(0.05)
                async with database.session() as session:
                    for receipt in first.receipts:
                        task = await session.get(AITask, UUID(receipt.task_id))
                        assert task is not None
                        evidence = await LedgerService(session).evidence_for_task(task.id)
                        assert len(evidence) == 1
                        print(
                            f"{task.title} | source={task.source.value} "
                            f"| state={task.status.value}",
                            flush=True,
                        )
                        print(
                            f"OpsEvent={receipt.event_id} | Task={task.id} "
                            f"| Evidence={evidence[0].id}",
                            flush=True,
                        )
                        summary = evidence[0].result_snapshot
                        if isinstance(summary, dict) and summary.get("predicted_exhaustion_at"):
                            print(
                                f"预计耗尽时间（UTC）：{summary['predicted_exhaustion_at']}",
                                flush=True,
                            )
                duplicate = await trigger()
                assert {r.task_id for r in duplicate.receipts} == {
                    r.task_id for r in first.receipts
                }
                assert all(r.duplicate for r in duplicate.receipts)
                print("持续异常复测：新增任务 0，五个任务 ID 保持一致。", flush=True)
                healthy = True
                normal = await trigger()
                assert normal.checked == 5 and normal.receipts == []
                print("健康复测：副本与四类趋势正常，新增事件/任务 0。", flush=True)
                print(
                    "Step 23 Fake 演示通过：1 个 State + 4 个 Prediction，"
                    "均有证据和实际任务 Workflow。",
                    flush=True,
                )
    finally:
        if created:
            await client.get_schedule_handle(settings.detection_config.schedule_id).delete()
        for receipt in receipts:
            await client.get_workflow_handle(receipt.workflow_id).terminate("演示清理")
        await database.dispose()
