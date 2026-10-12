import asyncio

from temporalio.client import Client
from temporalio.worker import Replayer

from app.tasks.workflow import AITaskWorkflow


async def main():
    client = await Client.connect("127.0.0.1:7233")
    count = 0
    async for execution in client.list_workflows(
        'WorkflowType = "AITaskWorkflow" AND ExecutionStatus = "Completed"'
    ):
        count += 1
        history = await client.get_workflow_handle(execution.id, run_id=execution.run_id).fetch_history()
        markers = b" ".join(
            payload.data
            for event in history.events
            if event.HasField("marker_recorded_event_attributes")
            for payloads in event.marker_recorded_event_attributes.details.values()
            for payload in payloads.payloads
        )
        if b"action-plan-v1" in markers and b"action-approval-v1" not in markers:
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(history)
            print(f"Step 30 接入前 Action Plan 完成历史 Replay 通过：{execution.id}")
            return
        if count >= 1000:
            break
    raise RuntimeError("未找到接入前的 Action Plan 完成历史")


asyncio.run(main())
