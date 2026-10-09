"""状态和预测共用一个 Temporal Schedule；保留已有暂停状态与配置。"""

from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)

from app.triggers.detection.config import DetectionConfig
from app.triggers.detection.models import DetectionInput
from app.triggers.detection.workflow import StatePredictionWorkflow


def detection_schedule(config: DetectionConfig, task_queue: str) -> Schedule:
    config = DetectionConfig.model_validate(config)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            StatePredictionWorkflow.run,
            DetectionInput(config.activity_timeout_seconds, config.activity_max_attempts),
            id=f"{config.schedule_id}-run",
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=config.interval_seconds))],
            time_zone_name="UTC",
        ),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=timedelta(seconds=config.interval_seconds),
            pause_on_failure=True,
        ),
    )


async def ensure_detection_schedule(
    client: Client, config: DetectionConfig, task_queue: str
) -> bool:
    config = DetectionConfig.model_validate(config)
    if not config.enabled or not (config.state_rules or config.trend_rules):
        return False
    try:
        await client.create_schedule(config.schedule_id, detection_schedule(config, task_queue))
        return True
    except ScheduleAlreadyRunningError:
        return False
