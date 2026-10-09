"""发布参数、只读边界、动作凭证及窗口判据的禁网络验收。"""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.connectors.changes.models import CodeComparison, ConfigComparison, FileDiff
from app.connectors.changes.releases import FakeReleaseReader, FakeReleaseState
from app.connectors.kubernetes.execution import fake_binding
from app.db.base import utc_now
from app.executor.models import ExecutionCommand, ExecutionTarget
from app.policy.models import RiskLevel
from app.tasks.releases.demo import demo_settings
from app.tasks.releases.models import (
    DeployParameters,
    ReleaseConfig,
    ReleaseManifest,
    ReleaseWindow,
)
from app.tasks.releases.service import recovery_checks, sql_risk
from app.tasks.states import TaskStatus
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import WorkflowInput
from app.tools.changes import CompareVersionsOutput

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("forbid_llm_network")]


@pytest.mark.parametrize(
    "scenario,healthy",
    [("normal", True), ("anomaly", False), ("high_sql", True), ("deteriorating", False)],
)
async def test_fake_reader_observation_scoped_and_read_only(scenario: str, healthy: bool) -> None:
    state = FakeReleaseState(scenario=scenario)
    state.add("release-test")
    target = state.writer.targets["payment-service"]
    state.writer.targets["payment-service"] = target.model_copy(
        update={"image": fake_binding().images["v2.3.7"]}
    )
    end = utc_now()
    reader = FakeReleaseReader(state)
    observed = await reader.observe(
        ReleaseWindow(release_id="release-test", start=end - timedelta(seconds=1), end=end)
    )
    assert all(recovery_checks(observed, demo_settings("unit")).values()) is healthy
    assert observed.start.utcoffset() == timedelta(0) and observed.end == end
    assert not hasattr(reader, "execute") and not hasattr(reader, "issue")
    assert state.writer.issue_count == state.writer.execution_count == 0


@pytest.mark.parametrize("percent", [0, 101, True])
async def test_invalid_gray_percent_rejected(percent: int) -> None:
    with pytest.raises(ValueError):
        DeployParameters(from_version="v2.3.6", to_version="v2.3.7", traffic_percent=percent)


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
async def test_invalid_window_configuration_rejected(seconds: float) -> None:
    with pytest.raises(ValueError):
        ReleaseConfig(observation_seconds=seconds)


async def test_future_and_empty_windows_rejected() -> None:
    state = FakeReleaseState()
    state.add("release-test")
    now = utc_now()
    with pytest.raises(ValueError):
        ReleaseWindow(release_id="release-test", start=now, end=now)
    with pytest.raises(ValueError):
        await FakeReleaseReader(state).observe(
            ReleaseWindow(release_id="release-test", start=now, end=now + timedelta(seconds=1))
        )


@pytest.mark.parametrize(
    "mixed",
    [
        {"ticket_id": "ticket-x"},
        {"waits": [TaskStatus.WAITING_INFORMATION]},
        {"investigation_json": "{}"},
    ],
)
async def test_release_input_cannot_mix_scenarios(mixed: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_workflow_input(WorkflowInput(str(uuid4()), release_id="release-test", **mixed))  # type: ignore[arg-type]


async def test_paused_and_gray_defaults_keep_legacy_snapshot_shape() -> None:
    state = FakeReleaseState()
    target = state.writer.targets["payment-service"]
    assert "paused" not in target.model_dump(
        mode="json"
    ) and "traffic_percent" not in target.model_dump(mode="json")
    assert ExecutionTarget.model_validate_json(target.model_dump_json()) == target


async def test_release_credentials_exact_scope_expiry_and_idempotency() -> None:
    state = FakeReleaseState()
    now = utc_now()
    state.writer.clock = lambda: now
    target = state.writer.targets["payment-service"]
    command = ExecutionCommand(
        execution_id=uuid4(),
        task_id=uuid4(),
        plan_evidence_id=uuid4(),
        plan_hash="a" * 64,
        action_id="release-canary",
        name="deploy_service",
        target=target,
        expected_image=fake_binding().images["v2.3.7"],
        expected_replicas=target.replicas,
        expected_paused=False,
        expected_traffic_percent=10,
    )
    token = await state.writer.issue(command, 1)
    with pytest.raises(PermissionError):
        await state.writer.execute(
            command.model_copy(update={"expected_traffic_percent": 100}), token
        )
    receipt = await state.writer.execute(command, token)
    assert (await state.writer.execute(command, token)) == receipt
    assert state.writer.execution_count == 1 and receipt.target.traffic_percent == 10
    now += timedelta(seconds=1)
    with pytest.raises(PermissionError):
        await state.writer.execute(command, token)


async def test_pause_command_cannot_change_image_or_traffic() -> None:
    target = FakeReleaseState().writer.targets["payment-service"]
    with pytest.raises(ValueError):
        ExecutionCommand(
            execution_id=uuid4(),
            task_id=uuid4(),
            plan_evidence_id=uuid4(),
            plan_hash="a" * 64,
            action_id="release-pause",
            name="pause_release",
            target=target,
            expected_image=fake_binding().images["v2.3.7"],
            expected_replicas=target.replicas,
            expected_paused=True,
            expected_traffic_percent=100,
        )


@pytest.mark.parametrize(
    "sql,path,patch,expected",
    [
        ((), "config.py", "+pool = 500", RiskLevel.L0),
        (("ALTER TABLE payments ADD COLUMN memo TEXT",), "config.py", "", RiskLevel.L4),
        (("DROP TABLE payments",), "config.py", "", RiskLevel.L5),
        ((), "migrations/01.sql", "+SELECT 1", RiskLevel.L4),
        ((), "app.py", "+cursor.execute('TRUNCATE payments')", RiskLevel.L5),
    ],
)
async def test_sql_material_and_embedded_diff_are_conservatively_flagged(
    sql: tuple[str, ...], path: str, patch: str, expected: RiskLevel
) -> None:
    manifest = ReleaseManifest(
        release_id="release-sql",
        service_name="payment-service",
        from_version="v2.3.6",
        to_version="v2.3.7",
        sql=sql,
        resource_ready=True,
        monitoring_ready=True,
        rollback_ready=True,
        reference="fake://release-sql",
    )
    query = {"service_name": "payment-service", "from_version": "v2.3.6", "to_version": "v2.3.7"}
    diff = CompareVersionsOutput(
        **query,
        code=CodeComparison(
            **query,
            source="gitlab",
            repository="repo/payment",
            files=(FileDiff(old_path=path, new_path=path, patch=patch, status="modified"),),
            comparison_kind="direct",
            source_ref="fake://git/diff",
        ),
        configuration=ConfigComparison(
            **query, source="config_center", changes=(), source_ref="fake://config/diff"
        ),
    )
    assert sql_risk(manifest, diff) is expected
