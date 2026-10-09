"""Webhook 与 Watch 的 Temporal 入口；不在 API 内自建队列或重试。"""

import asyncio
from uuid import uuid4

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.config import Settings
from app.triggers.schemas import EventBatch, EventReceipt, NormalizedEvent, WatchInput
from app.triggers.workflow import EventIngestionWorkflow, KubernetesEventWatchWorkflow


class EventGateway:
    def __init__(self, client: Client, settings: Settings) -> None:
        self.client, self.settings = client, settings

    async def submit(self, events: list[NormalizedEvent]) -> list[EventReceipt]:
        if not events:
            return []
        handle = await self.client.start_workflow(
            EventIngestionWorkflow.run,
            EventBatch([event.model_dump_json() for event in events]),
            id=f"ops-ingest-{uuid4()}",
            task_queue=self.settings.temporal_config.task_queue,
        )
        return await asyncio.wait_for(
            handle.result(), self.settings.trigger_config.response_timeout_seconds
        )


async def ensure_event_watchers(client: Client, settings: Settings) -> None:
    if not settings.trigger_config.watcher_enabled:
        return
    cluster = settings.kubernetes_config.cluster_name if settings.kubernetes_config else "ack-fake"
    for namespace in settings.trigger_config.watcher_namespaces:
        try:
            await client.start_workflow(
                KubernetesEventWatchWorkflow.run,
                WatchInput(namespace),
                id=f"k8s-event-watch-{cluster}-{namespace}",
                task_queue=settings.temporal_config.task_queue,
            )
        except WorkflowAlreadyStartedError:
            pass
