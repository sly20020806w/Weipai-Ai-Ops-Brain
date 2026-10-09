"""与 API 共用后端包的第二入口；Step 17 仅注册本地占位实现。"""

import asyncio
from datetime import timedelta

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.worker import Worker

from app.agent.activities import AgentActivities
from app.agent.chat.activities import ChatActivities
from app.agent.chat.workflow import ChatIngestionWorkflow
from app.agent.reviewer.activities import ReviewerActivities
from app.config import Settings
from app.connectors.feishu.base import FeishuConnector
from app.db.session import Database
from app.executor.activities import ExecutorActivities
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.workflow import ChangeTimelineWorkflow
from app.graph.discovery.activities import DiscoveryActivities
from app.graph.discovery.schedule import ensure_discovery_schedule
from app.graph.discovery.workflow import DiscoveryWorkflow
from app.learning.activities import LearningActivities
from app.learning.automation.activities import AutomationActivities
from app.learning.automation.schedule import ensure_automation_schedule
from app.learning.automation.workflow import AutomationDiscoveryWorkflow
from app.learning.evaluation.activities import ReplayActivities
from app.learning.evaluation.workflow import ReplayEvaluationWorkflow
from app.runbooks.activities import RunbookActivities
from app.tasks.activities import TaskActivities, TaskActivityStore
from app.tasks.approval.activities import ApprovalActivities
from app.tasks.architecture.activities import ArchitectureActivities
from app.tasks.config import TemporalConfig
from app.tasks.control import ControlActivities
from app.tasks.control_workflow import TaskControlWorkflow
from app.tasks.human.activities import HumanActivities
from app.tasks.inspection.activities import InspectionActivities
from app.tasks.inspection.workflow import InspectionWorkflow
from app.tasks.planning.activities import PlanningActivities
from app.tasks.releases.activities import ReleaseActivities
from app.tasks.safety.activities import SafetyActivities
from app.tasks.states import TaskStatus
from app.tasks.tickets.activities import TicketActivities
from app.tasks.war_room.activities import WarRoomActivities
from app.tasks.workflow import AITaskWorkflow, validate_workflow_input
from app.tasks.workflow_models import WorkflowInput, WorkflowProgress
from app.triggers.activities import EventActivities
from app.triggers.detection.activities import DetectionActivities
from app.triggers.detection.schedule import ensure_detection_schedule
from app.triggers.detection.workflow import StatePredictionWorkflow
from app.triggers.gateway import ensure_event_watchers
from app.triggers.scheduling.schedule import ensure_periodic_schedules
from app.triggers.scheduling.workflow import (
    PeriodicTriggerWorkflow,
    ReleaseVerificationTriggerWorkflow,
)
from app.triggers.workflow import EventIngestionWorkflow, KubernetesEventWatchWorkflow
from app.verifier.activities import VerifierActivities
from app.verifier.architecture import ArchitectureVerifier
from app.verifier.chat import ChatVerifier
from app.verifier.inspection import InspectionVerifier
from app.verifier.placeholder import PlaceholderVerifier
from app.verifier.war_room import WarRoomVerifier


def validate_placeholder_settings(settings: Settings) -> None:
    # 禁止将尚未接入真实 Policy/Executor/Verifier 的占位闭环用于生产。
    if settings.app_env not in {"local", "test"}:
        raise ValueError("Step 17 占位 Worker 仅允许 APP_ENV=local/test")
    if settings.connector_mode.value != "fake" or settings.llm_mode != "fake":
        raise ValueError("Step 17 占位 Worker 要求 Fake Connector 与 Fake LLM")
    if settings.temporal_config.address.rpartition(":")[0] not in {
        "127.0.0.1",
        "localhost",
        "[::1]",
    }:
        raise ValueError("local/test Temporal 只允许本机回环地址")


def create_worker(
    client: Client,
    database: Database,
    settings: Settings,
    *,
    max_cached_workflows: int = 1000,
    feishu_connector: FeishuConnector | None = None,
    verifier_activities: VerifierActivities | None = None,
    executor_activities: ExecutorActivities | None = None,
    learning_activities: LearningActivities | None = None,
    ticket_activities: TicketActivities | None = None,
    release_activities: ReleaseActivities | None = None,
    inspection_activities: InspectionActivities | None = None,
    architecture_activities: ArchitectureActivities | None = None,
    war_room_activities: WarRoomActivities | None = None,
) -> Worker:
    validate_placeholder_settings(settings)
    store = TaskActivityStore(database)
    activities = TaskActivities(store)
    control = ControlActivities(database, settings, client)
    verifier = PlaceholderVerifier(store, app_env=settings.app_env)
    events = EventActivities(database, settings, client)
    detection = DetectionActivities(database, settings)
    agent = AgentActivities(database, settings)
    human = HumanActivities(database, settings, connector=feishu_connector)
    approvals = ApprovalActivities(database, settings, connector=feishu_connector)
    safety = SafetyActivities(database, settings, connector=feishu_connector)
    tickets = ticket_activities or TicketActivities(database, settings)
    releases = release_activities or ReleaseActivities(database, settings)
    inspections = inspection_activities or InspectionActivities(
        database, settings, feishu=feishu_connector
    )
    executor = executor_activities or ExecutorActivities(database, settings)
    war_room = war_room_activities or WarRoomActivities(
        database, settings, resources=executor.connector
    )
    return Worker(
        client,
        task_queue=settings.temporal_config.task_queue,
        workflows=[
            ChatIngestionWorkflow,
            TaskControlWorkflow,
            AITaskWorkflow,
            DiscoveryWorkflow,
            ChangeTimelineWorkflow,
            EventIngestionWorkflow,
            KubernetesEventWatchWorkflow,
            PeriodicTriggerWorkflow,
            ReleaseVerificationTriggerWorkflow,
            StatePredictionWorkflow,
            ReplayEvaluationWorkflow,
            AutomationDiscoveryWorkflow,
            InspectionWorkflow,
        ],
        activities=[
            ChatActivities(database).persist,
            ChatVerifier(database, settings).verify,
            control.record,
            control.deliver,
            war_room.input,
            war_room.interval,
            war_room.assess,
            war_room.review,
            war_room.plan,
            war_room.report,
            WarRoomVerifier(database, settings, war_room.facts, war_room.resources).verify,
            (architecture_activities or ArchitectureActivities(database, settings)).review,
            ArchitectureVerifier(database, settings).verify,
            inspections.scan,
            inspections.notify,
            InspectionVerifier(database, settings).verify,
            releases.match_runbook,
            releases.read_assessment,
            releases.assess,
            releases.review,
            releases.plan,
            releases.execute,
            releases.observe,
            releases.report,
            tickets.prepare,
            tickets.investigate,
            tickets.conclude,
            tickets.review,
            tickets.plan,
            tickets.execute,
            tickets.verify,
            tickets.learn,
            activities.load,
            activities.transition,
            activities.placeholder_stage,
            verifier.verify,
            (verifier_activities or VerifierActivities(database, settings)).verify,
            (verifier_activities or VerifierActivities(database, settings)).prepare,
            DiscoveryActivities(database, settings).refresh,
            TimelineActivities(database, settings).collect,
            events.persist,
            events.start_task,
            events.watch_kubernetes,
            detection.collect,
            detection.persist,
            agent.investigate,
            agent.validate_conclusion,
            ReviewerActivities(database, settings).review,
            RunbookActivities(database, settings).match,
            PlanningActivities(database, settings).plan,
            human.notify,
            human.record_answer,
            approvals.notify,
            approvals.decide,
            safety.check,
            safety.notify,
            executor.execute,
            (learning_activities or LearningActivities(database, settings)).generate,
            ReplayActivities(database, settings).replay,
            AutomationActivities(database, settings).scan,
        ],
        graceful_shutdown_timeout=timedelta(seconds=10),
        max_cached_workflows=max_cached_workflows,
    )


async def start_task_workflow(
    client: Client, value: WorkflowInput, *, task_queue: str
) -> WorkflowHandle[AITaskWorkflow, WorkflowProgress]:
    validate_workflow_input(value)
    return await client.start_workflow(
        AITaskWorkflow.run,
        value,
        id=f"ai-task-{value.task_id}",
        task_queue=task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )


def configured_workflow_input(
    task_id: str, config: TemporalConfig, *, waits: list[TaskStatus] | None = None
) -> WorkflowInput:
    value = WorkflowInput(
        task_id,
        waits=list(waits or []),
        human_timeout_seconds=config.human_timeout_seconds,
        activity_timeout_seconds=config.activity_timeout_seconds,
        activity_max_attempts=config.activity_max_attempts,
    )
    validate_workflow_input(value)
    return value


async def run_worker(settings: Settings) -> None:
    validate_placeholder_settings(settings)
    database = Database(settings.require_database_url())
    try:
        client = await Client.connect(
            settings.temporal_config.address, namespace=settings.temporal_config.namespace
        )
        worker = create_worker(client, database, settings)
        await ensure_event_watchers(client, settings)
        await ensure_discovery_schedule(
            client, settings.discovery_config, settings.temporal_config.task_queue
        )
        await ensure_periodic_schedules(
            client, settings.scheduling_config, settings.temporal_config.task_queue
        )
        await ensure_detection_schedule(
            client, settings.detection_config, settings.temporal_config.task_queue
        )
        await ensure_automation_schedule(
            client, settings.automation_config, settings.temporal_config.task_queue
        )
        print(
            f"占位 Worker 已启动：namespace={client.namespace} "
            f"task_queue={settings.temporal_config.task_queue}",
            flush=True,
        )
        await worker.run()
    finally:
        await database.dispose()


def main() -> None:
    try:
        asyncio.run(run_worker(Settings()))
    except KeyboardInterrupt:
        print("占位 Worker 已停止；未完成任务由 Temporal 保留", flush=True)


if __name__ == "__main__":
    main()
