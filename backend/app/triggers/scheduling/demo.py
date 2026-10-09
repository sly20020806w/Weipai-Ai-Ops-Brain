"""可自行运行的 Fake 演示，展示实际 Schedule、数据库事件与 Task Workflow。"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client, ScheduleActionExecutionStartWorkflow

from app.config import Settings
from app.db.session import Database
from app.tasks.config import TemporalConfig
from app.tasks.models import AITask
from app.tasks.states import TaskSource
from app.tasks.worker import create_worker
from app.triggers.models import OpsEvent
from app.triggers.scheduling.config import SchedulingConfig
from app.triggers.scheduling.schedule import ensure_periodic_schedules
from app.triggers.schemas import EventBatch, EventReceipt, NormalizedEvent
from app.triggers.workflow import EventIngestionWorkflow


async def run_demo(url: URL) -> None:
    token = uuid4().hex
    config = Settings(
        APP_ENV="test",
        TEMPORAL_CONFIG=TemporalConfig(
            address=os.environ["TEST_TEMPORAL_ADDRESS"], task_queue=f"scheduling-demo-{token}"
        ),
        SCHEDULING_CONFIG=SchedulingConfig(
            schedule_prefix=f"weipai-demo-{token}", release_delay_seconds=2
        ),
    )
    database = Database(url)
    client = await Client.connect(config.temporal_config.address, namespace="default")
    ids: list[str] = []
    receipts: list[EventReceipt] = []
    try:
        ids = await ensure_periodic_schedules(
            client, config.scheduling_config, config.temporal_config.task_queue
        )
        for name in ids:
            await client.get_schedule_handle(name).pause(note="演示只即时触发一次")
        print("已注册 3 个隔离演示 Schedule：", flush=True)
        for name in ids:
            print(name, flush=True)
        async with create_worker(client, database, config):
            for name in ids:
                handle = client.get_schedule_handle(name)
                await handle.trigger()
                async with asyncio.timeout(30):
                    while not (description := await handle.describe()).info.recent_actions:
                        await asyncio.sleep(0.05)
                action = description.info.recent_actions[0].action
                assert isinstance(action, ScheduleActionExecutionStartWorkflow)
                receipt = await asyncio.wait_for(
                    client.get_workflow_handle(
                        action.workflow_id, result_type=EventReceipt
                    ).result(),
                    30,
                )
                receipts.append(receipt)
            print(
                "发布验证演示使用隔离配置 delay=2 秒；默认 600 秒由时间跳跃专项测试验证。",
                flush=True,
            )
            release = NormalizedEvent(
                origin="argocd",
                source=TaskSource.RELEASE,
                external_id=f"demo-{token}",
                service_name="payment-service",
                title="payment-service 发布 v2.3.7（Fake）",
                occurred_at=datetime.now(UTC),
            )
            original = (
                await client.execute_workflow(
                    EventIngestionWorkflow.run,
                    EventBatch([release.model_dump_json()]),
                    id=f"release-demo-{token}",
                    task_queue=config.temporal_config.task_queue,
                )
            )[0]
            receipts.append(original)
            receipt = await asyncio.wait_for(
                client.get_workflow_handle(
                    f"release-verification-{original.event_id}", result_type=EventReceipt
                ).result(),
                30,
            )
            receipts.append(receipt)
            async with database.session() as session:
                for receipt in receipts:
                    event = await session.get(OpsEvent, UUID(receipt.event_id))
                    task = await session.get(AITask, UUID(receipt.task_id))
                    assert event is not None and task is not None
                    print(
                        f"{event.title} | source={task.source.value} | OpsEvent={event.id} "
                        f"| Task={task.id} | occurred_at={event.occurred_at.isoformat()} "
                        f"| Workflow={receipt.workflow_id}",
                        flush=True,
                    )
            assert len(receipts) == 5
            print(
                "Step 22 Fake 演示通过：3 个周期任务 + 1 个发布任务 + 1 个上线验证任务。",
                flush=True,
            )
    finally:
        for name in ids:
            await client.get_schedule_handle(name).delete()
        for receipt in receipts:
            await client.get_workflow_handle(receipt.workflow_id).terminate("演示清理")
        await database.dispose()
