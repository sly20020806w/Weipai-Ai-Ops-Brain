"""只管理本平台的 Discovery Schedule；重复注册不新增或重置已有暂停状态。"""

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

from app.graph.discovery.config import DiscoveryConfig
from app.graph.discovery.models import DiscoveryInput
from app.graph.discovery.workflow import DiscoveryWorkflow


def discovery_input(config: DiscoveryConfig) -> DiscoveryInput:
    return DiscoveryInput(
        config.lookback_seconds, config.activity_timeout_seconds, config.activity_max_attempts
    )


def discovery_schedule(config: DiscoveryConfig, task_queue: str) -> Schedule:
    config = DiscoveryConfig.model_validate(config)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            DiscoveryWorkflow.run,
            discovery_input(config),
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


async def ensure_discovery_schedule(
    client: Client, config: DiscoveryConfig, task_queue: str
) -> bool:
    try:
        await client.create_schedule(config.schedule_id, discovery_schedule(config, task_queue))
        return True
    except ScheduleAlreadyRunningError:
        return False
