"""I/O Activity 只复用任务服务及只读 Connector。"""

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

from pydantic import ValidationError
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from app.agent.investigation import InvestigationSpec
from app.config import Settings
from app.connectors.kubernetes.factory import create_kubernetes_connector
from app.connectors.kubernetes.models import Deployment, Pod
from app.db.session import Database
from app.tasks.states import TaskSource, TaskStatus
from app.triggers.models import OpsEvent
from app.triggers.normalization import PayloadError, normalize_kubernetes
from app.triggers.scheduling.models import ReleaseVerificationInput
from app.triggers.scheduling.workflow import ReleaseVerificationTriggerWorkflow
from app.triggers.schemas import EventBatch, EventReceipt, NormalizedEvent, WatchInput, WatchResult
from app.triggers.service import EventService


class EventActivities:
    def __init__(self, database: Database, settings: Settings, client: Client) -> None:
        self.database, self.settings, self.client = database, settings, client

    @activity.defn(name="event.persist")
    async def persist(self, value: EventBatch) -> list[EventReceipt]:
        try:
            if not 1 <= len(value.events) <= 10000:
                raise ValueError("事件批次必须为 1–10000 条")
            events = [NormalizedEvent.model_validate_json(event) for event in value.events]
        except (ValueError, ValidationError):
            raise ApplicationError("归一化事件批次无效", non_retryable=True) from None
        async with self.database.session() as session, session.begin():
            return await EventService(session).accept(events)

    @activity.defn(name="event.start_task")
    async def start_task(self, value: EventReceipt) -> None:
        from app.tasks.worker import configured_workflow_input, start_task_workflow

        try:
            UUID(value.task_id)
            UUID(value.event_id)
            if value.workflow_id != f"ai-task-{value.task_id}":
                raise ValueError("任务 Workflow ID 不匹配")
            options = configured_workflow_input(
                value.task_id, self.settings.temporal_config, waits=[TaskStatus.WAITING_INFORMATION]
            )
        except ValueError:
            raise ApplicationError("任务派发参数无效", non_retryable=True) from None
        release_input = None
        async with self.database.session() as session:
            event = await session.get(OpsEvent, UUID(value.event_id))
            if event is None or str(event.task_id) != value.task_id:
                raise ApplicationError("触发事件与任务不匹配", non_retryable=True)
            periodic_kind = event.external_id.partition(":")[0]
            modes = {
                "workday-inspection": "inspection",
                "hourly-capacity": "capacity",
                "daily-governance": "governance",
            }
            if event.origin == "manual" and event.external_id.startswith("chat:"):
                from app.agent.chat.service import chat_input

                _, submission = await chat_input(session, UUID(value.task_id))
                spec = submission.input.spec(
                    submission.input.start or event.occurred_at - timedelta(hours=1),
                    submission.input.end or event.occurred_at,
                    self.settings.agent_config.max_steps,
                )
                options = replace(
                    options,
                    waits=[],
                    investigation_json=spec.model_dump_json(),
                    chat_mode=submission.input.mode,
                    execution_enabled=submission.input.mode == "task"
                    and self.settings.execution_config.enabled,
                )
            elif (
                event.origin == "manual"
                and event.source == TaskSource.HUMAN.value
                and event.external_id.startswith("war-room:")
                and self.settings.war_room_config.enabled
            ):
                options = replace(
                    options,
                    waits=[],
                    war_room=True,
                    execution_enabled=self.settings.execution_config.enabled,
                )
            elif (
                event.origin == "manual"
                and event.source == TaskSource.HUMAN.value
                and event.external_id.startswith("architecture-review:")
            ):
                options = replace(options, waits=[], architecture_review=True)
            elif (
                event.origin == "schedule"
                and event.source == TaskSource.SCHEDULE.value
                and periodic_kind in modes
                and self.settings.inspection_config.enabled
            ):
                options = replace(options, waits=[], inspection_mode=modes[periodic_kind])
            elif event.source == TaskSource.RELEASE.value and self.settings.release_config.enabled:
                options = replace(
                    options,
                    waits=[],
                    release_id=event.external_id,
                    execution_enabled=self.settings.execution_config.enabled,
                    release_observation_seconds=self.settings.release_config.observation_seconds,
                )
            elif event.source == TaskSource.TICKET.value and self.settings.ticket_config.enabled:
                options = replace(
                    options,
                    waits=[],
                    ticket_id=event.external_id,
                    execution_enabled=self.settings.execution_config.enabled,
                )
            elif self.settings.agent_config.enabled and event.origin != "learning":
                end = event.occurred_at + timedelta(microseconds=1)
                try:
                    spec = InvestigationSpec(
                        service_name=event.service_name,
                        title=event.title,
                        start=event.occurred_at - timedelta(hours=1),
                        end=end,
                        max_steps=self.settings.agent_config.max_steps,
                    )
                except ValidationError:
                    # K8s 未关联服务时用 namespace/kind/name 作定位，不是可查询服务名。
                    # 保留 CONTEXT_BUILDING 的持久化等待，不能让已接纳任务停在 NEW。
                    pass
                else:
                    options = replace(
                        options,
                        waits=[],
                        investigation_json=spec.model_dump_json(),
                        execution_enabled=self.settings.execution_config.enabled,
                    )
            if (
                event.source == TaskSource.RELEASE.value
                and not self.settings.release_config.enabled
            ):
                release_input = ReleaseVerificationInput(
                    str(event.id),
                    event.service_name,
                    event.occurred_at,
                    self.settings.scheduling_config.release_delay_seconds,
                )
        try:
            await start_task_workflow(
                self.client, options, task_queue=self.settings.temporal_config.task_queue
            )
        except WorkflowAlreadyStartedError:
            # 同一任务提交后丢响应／重复投递，不能启动第二个生命周期。
            pass
        # 与 start_task 同一 Activity：任一派发丢响应都由 Temporal 重试，固定 ID 去重。
        if release_input is None:
            return
        try:
            await self.client.start_workflow(
                ReleaseVerificationTriggerWorkflow.run,
                release_input,
                id=f"release-verification-{value.event_id}",
                task_queue=self.settings.temporal_config.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            pass

    @activity.defn(name="event.watch_kubernetes")
    async def watch_kubernetes(self, value: WatchInput) -> WatchResult:
        try:
            async with create_kubernetes_connector(self.settings) as connector:
                batch = await connector.watch_events(
                    value.namespace,
                    resource_version=value.resource_version,
                    timeout_seconds=self.settings.trigger_config.watch_timeout_seconds,
                )
                key = (
                    self.settings.kubernetes_config.service_label_key
                    if self.settings.kubernetes_config
                    else "app.kubernetes.io/name"
                )
                objects: tuple[Pod | Deployment, ...] = (
                    (
                        *await connector.list_pods(value.namespace),
                        *await connector.list_deployments(value.namespace),
                    )
                    if batch.events
                    else ()
                )
                services = {item.metadata.uid: item.metadata.labels.get(key) for item in objects}
                events = [
                    normalize_kubernetes(
                        connector.cluster_name,
                        event,
                        service_name=services.get(event.involved_object.uid or ""),
                    )
                    for event in batch.events
                ]
            return WatchResult(
                [event.model_dump_json() for event in events if event is not None],
                batch.resource_version,
            )
        except (PayloadError, ValidationError):
            raise ApplicationError("K8s Watch 事件无效", non_retryable=True) from None
