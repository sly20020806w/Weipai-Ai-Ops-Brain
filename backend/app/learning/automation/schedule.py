"""周期发现重复劳动；重复注册保留已有 Schedule 配置和暂停状态。"""

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

from app.learning.automation.models import AutomationConfig, AutomationInput
from app.learning.automation.workflow import AutomationDiscoveryWorkflow


def automation_schedule(config: AutomationConfig, task_queue: str) -> Schedule:
    config = AutomationConfig.model_validate(config)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            AutomationDiscoveryWorkflow.run,
            AutomationInput(config.activity_timeout_seconds, config.activity_max_attempts),
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


async def ensure_automation_schedule(
    client: Client, config: AutomationConfig, task_queue: str
) -> bool:
    config = AutomationConfig.model_validate(config)
    if not config.enabled:
        return False
    try:
        await client.create_schedule(config.schedule_id, automation_schedule(config, task_queue))
        return True
    except ScheduleAlreadyRunningError:
        return False
