"""本地可复制的 Workflow 演示：普通闭环、等待后回答、等待超时。"""

import argparse
import asyncio
from dataclasses import replace
from uuid import UUID

from temporalio.client import Client

from app.config import Settings
from app.db.session import Database
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.worker import (
    configured_workflow_input,
    start_task_workflow,
    validate_placeholder_settings,
)
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanResponse


async def run_demo(mode: str) -> None:
    settings = Settings()
    validate_placeholder_settings(settings)
    database = Database(settings.require_database_url())
    try:
        client = await Client.connect(
            settings.temporal_config.address, namespace=settings.temporal_config.namespace
        )
        async with database.session() as session, session.begin():
            task = await TaskService(session).create(
                source=TaskSource.HUMAN,
                title=f"Step 17 本地占位演示：{mode}",
                reason="人工验收请求",
            )
        waits = [] if mode == "normal" else [TaskStatus.WAITING_APPROVAL]
        value = configured_workflow_input(str(task.id), settings.temporal_config, waits=waits)
        if mode == "timeout":
            value = replace(value, human_timeout_seconds=1)
        handle = await start_task_workflow(
            client, value, task_queue=settings.temporal_config.task_queue
        )
        print(f"Workflow ID：{handle.id}；请在本地 Temporal UI 查看", flush=True)
        if mode == "signal":
            # 仅演示客户端观察；业务暂停由 Temporal 的 wait_condition 实现。
            async with asyncio.timeout(30):
                while True:
                    progress = await handle.query(AITaskWorkflow.progress)
                    current = progress.task
                    if current is not None and current.status is TaskStatus.WAITING_APPROVAL:
                        print(f"已暂停：{current.status}，版本 {current.version}", flush=True)
                        await handle.signal(
                            AITaskWorkflow.human_response,
                            HumanResponse(current.status, current.version, True),
                        )
                        print("已发送匹配当前等待版本的演示信号", flush=True)
                        break
                    await asyncio.sleep(0.1)
        result = await asyncio.wait_for(handle.result(), timeout=60)
        async with database.session() as session:
            history = await TaskService(session).history(UUID(value.task_id))
            assert [(entry.to_status, entry.sequence) for entry in history] == [
                (entry.status, entry.version) for entry in result.history
            ]
        expected = TaskStatus.ESCALATED if mode == "timeout" else TaskStatus.CLOSED
        assert result.task is not None and result.task.status is expected
        print("数据库历史：" + " → ".join(entry.to_status.value for entry in history), flush=True)
        print(f"演示通过：最终状态 {expected}，数据库与 Workflow 记录一致", flush=True)
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 17 本地占位 Workflow 演示")
    parser.add_argument(
        "mode", choices=("normal", "signal", "timeout"), default="normal", nargs="?"
    )
    asyncio.run(run_demo(parser.parse_args().mode))


if __name__ == "__main__":
    main()
