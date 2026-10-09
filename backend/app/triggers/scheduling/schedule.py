"""只注册三个自有 Schedule；已有 Schedule 的暂停状态与配置保持不变。"""

from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
)

from app.triggers.scheduling.config import SchedulingConfig
from app.triggers.scheduling.models import PERIODIC_TITLES, PeriodicInput, PeriodicKind
from app.triggers.scheduling.workflow import PeriodicTriggerWorkflow


def periodic_schedules(config: SchedulingConfig, task_queue: str) -> dict[str, Schedule]:
    config = SchedulingConfig.model_validate(config)
    if not config.enabled:
        return {}
    calendars: dict[PeriodicKind, ScheduleCalendarSpec] = {
        "workday-inspection": ScheduleCalendarSpec(
            hour=[ScheduleRange(config.inspection_hour)],
            minute=[ScheduleRange(config.inspection_minute)],
            day_of_week=[ScheduleRange(day) for day in config.inspection_weekdays],
        ),
        "hourly-capacity": ScheduleCalendarSpec(
            hour=[ScheduleRange(0, 23)], minute=[ScheduleRange(config.capacity_minute)]
        ),
        "daily-governance": ScheduleCalendarSpec(
            hour=[ScheduleRange(config.governance_hour)],
            minute=[ScheduleRange(config.governance_minute)],
        ),
    }
    return {
        f"{config.schedule_prefix}-{kind}": Schedule(
            action=ScheduleActionStartWorkflow(
                PeriodicTriggerWorkflow.run,
                PeriodicInput(kind, config.service_name),
                id=f"{config.schedule_prefix}-{kind}-run",
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(calendars=[calendar], time_zone_name=config.time_zone),
            policy=SchedulePolicy(
                overlap=ScheduleOverlapPolicy.SKIP,
                catchup_window=timedelta(seconds=config.catchup_window_seconds),
                pause_on_failure=True,
            ),
        )
        for kind, calendar in calendars.items()
        if kind in PERIODIC_TITLES
    }


async def ensure_periodic_schedules(
    client: Client, config: SchedulingConfig, task_queue: str
) -> list[str]:
    created = []
    for schedule_id, schedule in periodic_schedules(config, task_queue).items():
        try:
            await client.create_schedule(schedule_id, schedule)
            created.append(schedule_id)
        except ScheduleAlreadyRunningError:
            pass
    return created
