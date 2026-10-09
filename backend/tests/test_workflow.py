"""Step 17：输入、序列化、信号版本与占位运行门禁。"""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from temporalio.converter import DataConverter

from app.config import Settings
from app.tasks.config import TemporalConfig
from app.tasks.states import TaskStatus
from app.tasks.worker import configured_workflow_input, validate_placeholder_settings
from app.tasks.workflow import AITaskWorkflow, validate_workflow_input
from app.tasks.workflow_models import HumanResponse, TaskSnapshot, WorkflowInput, WorkflowProgress


@pytest.mark.parametrize(
    "value",
    [
        WorkflowInput("invalid"),
        WorkflowInput(str(uuid4()), waits=[TaskStatus.EXECUTING]),
        WorkflowInput(str(uuid4()), waits=[TaskStatus.WAITING_APPROVAL] * 2),
        WorkflowInput(str(uuid4()), human_timeout_seconds=0),
        WorkflowInput(str(uuid4()), human_timeout_seconds=float("nan")),
        WorkflowInput(str(uuid4()), activity_timeout_seconds=float("inf")),
        WorkflowInput(str(uuid4()), activity_max_attempts=0),
        WorkflowInput(str(uuid4()), activity_max_attempts=11),
    ],
)
def test_invalid_input_rejected(value: WorkflowInput) -> None:
    with pytest.raises(ValueError):
        validate_workflow_input(value)


@pytest.mark.asyncio
async def test_temporal_dataclass_roundtrip() -> None:
    converter = DataConverter.default
    task = TaskSnapshot(str(uuid4()), TaskStatus.WAITING_APPROVAL, 6)
    for value in (
        WorkflowInput(task.task_id, waits=[TaskStatus.WAITING_APPROVAL]),
        HumanResponse(TaskStatus.WAITING_APPROVAL, 6, True),
        WorkflowProgress(task, [task]),
    ):
        payload = await converter.encode([value])
        assert (await converter.decode(payload, [type(value)]))[0] == value


def test_signal_requires_matching_wait_and_first_response_wins() -> None:
    instance = AITaskWorkflow()
    instance.task = TaskSnapshot(str(uuid4()), TaskStatus.WAITING_APPROVAL, 6)
    response = HumanResponse(TaskStatus.WAITING_APPROVAL, 6, False)
    instance.human_response(replace(response, wait_status=TaskStatus.NEED_HUMAN_JUDGMENT))
    instance.human_response(replace(response, wait_version=5))
    assert instance.response is None
    instance.human_response(response)
    instance.human_response(replace(response, accepted=True))
    assert instance.response == response
    instance.response = None
    instance.task = replace(instance.task, status=TaskStatus.EXECUTING)
    instance.human_response(response)
    assert instance.response is None


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_placeholder_worker_cannot_run_outside_local_test(environment: str) -> None:
    settings = Settings.model_validate({"APP_ENV": environment})
    with pytest.raises(ValueError, match="仅允许"):
        validate_placeholder_settings(settings)


@pytest.mark.parametrize("address", ["temporal.company:7233", "10.0.0.1:7233"])
def test_local_worker_cannot_contact_remote_temporal(address: str) -> None:
    settings = Settings(APP_ENV="local", TEMPORAL_CONFIG=TemporalConfig(address=address))
    with pytest.raises(ValueError, match="回环"):
        validate_placeholder_settings(settings)


@pytest.mark.parametrize("address", ["https://x:7233", "user:secret@x:7233", "x:0", "x:65536", "x"])
def test_temporal_address_rejects_invalid_or_credential_values(address: str) -> None:
    with pytest.raises(ValidationError):
        TemporalConfig(address=address)


def test_temporal_config_is_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv(
        "TEMPORAL_CONFIG", '{"task_queue":"isolated-tasks","human_timeout_seconds":12}'
    )
    settings = Settings()
    assert settings.temporal_config.task_queue == "isolated-tasks"
    assert settings.temporal_config.human_timeout_seconds == 12
    value = configured_workflow_input(str(uuid4()), settings.temporal_config)
    assert value.human_timeout_seconds == 12
    assert value.activity_max_attempts == settings.temporal_config.activity_max_attempts
