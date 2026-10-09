"""离线检查日历契约、配置门禁与注册幂等；只使用 Fake Client。"""

from typing import cast

import pytest
from pydantic import ValidationError
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleRange,
)

from app.config import Settings
from app.triggers.scheduling.config import SchedulingConfig
from app.triggers.scheduling.models import PeriodicInput
from app.triggers.scheduling.schedule import ensure_periodic_schedules, periodic_schedules


@pytest.mark.parametrize(
    "changes",
    [
        {"inspection_hour": 24},
        {"governance_hour": -1},
        {"capacity_minute": 60},
        {"inspection_weekdays": ()},
        {"inspection_weekdays": (1, 1)},
        {"inspection_weekdays": (7,)},
        {"release_delay_seconds": 0},
        {"release_delay_seconds": 86401},
        {"catchup_window_seconds": 0},
        {"schedule_prefix": "unsafe/prefix"},
        {"service_name": " "},
        {"time_zone": "unknown"},
        {"unknown_option": True},
    ],
)
def test_bad_configuration_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SchedulingConfig.model_validate(changes)


def test_default_three_calendars_and_conservative_policy() -> None:
    config = SchedulingConfig()
    schedules = periodic_schedules(config, "queue")
    assert list(schedules) == [
        "weipai-ops-workday-inspection",
        "weipai-ops-hourly-capacity",
        "weipai-ops-daily-governance",
    ]
    calendars = [schedule.spec.calendars[0] for schedule in schedules.values()]
    assert calendars[0].hour == [ScheduleRange(9)]
    assert calendars[0].day_of_week == [ScheduleRange(day) for day in range(1, 6)]
    assert calendars[1].hour == [ScheduleRange(0, 23)]
    assert calendars[2].hour == [ScheduleRange(18)]
    assert config.release_delay_seconds == 600
    for schedule in schedules.values():
        assert schedule.spec.time_zone_name == "Asia/Shanghai"
        assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
        assert schedule.policy.pause_on_failure
        assert schedule.policy.catchup_window.total_seconds() == 3600
        assert isinstance(schedule.action, ScheduleActionStartWorkflow)
        assert schedule.action.task_queue == "queue"
        assert isinstance(schedule.action.args[0], PeriodicInput)


def test_configuration_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SCHEDULING_CONFIG",
        '{"time_zone":"UTC","inspection_hour":8,"capacity_minute":15,'
        '"governance_hour":22,"inspection_weekdays":[0,6]}',
    )
    config = Settings(APP_ENV="test").scheduling_config
    schedules = list(periodic_schedules(config, "queue").values())
    assert schedules[0].spec.time_zone_name == "UTC"
    assert schedules[0].spec.calendars[0].hour == [ScheduleRange(8)]
    assert schedules[0].spec.calendars[0].day_of_week == [ScheduleRange(0), ScheduleRange(6)]
    assert schedules[1].spec.calendars[0].minute == [ScheduleRange(15)]
    assert schedules[2].spec.calendars[0].hour == [ScheduleRange(22)]


class FakeScheduleClient:
    def __init__(self) -> None:
        self.schedules: dict[str, Schedule] = {}

    async def create_schedule(self, name: str, schedule: Schedule) -> None:
        if name in self.schedules:
            raise ScheduleAlreadyRunningError()
        self.schedules[name] = schedule


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid_llm_network")
async def test_registration_preserves_existing_pause_and_disabled_creates_nothing() -> None:
    fake = FakeScheduleClient()
    client = cast(Client, fake)
    config = SchedulingConfig()
    assert len(await ensure_periodic_schedules(client, config, "queue")) == 3
    original = next(iter(fake.schedules.values()))
    original.state.paused = True
    assert await ensure_periodic_schedules(client, config, "new-queue") == []
    assert original.state.paused
    assert isinstance(original.action, ScheduleActionStartWorkflow)
    assert original.action.task_queue == "queue"
    assert await ensure_periodic_schedules(client, SchedulingConfig(enabled=False), "q") == []
    assert len(fake.schedules) == 3
