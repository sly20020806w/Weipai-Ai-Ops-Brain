"""离线恢复标准、数据完整性和非 Verifier 状态门禁。"""

import json
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.connectors.cloud.fake import sample_resources
from app.connectors.cloud.models import CloudResources, RDSConnections
from app.connectors.kubernetes.verification_fake import verification_snapshot
from app.connectors.observability.verification_fake import (
    verification_logs,
    verification_metrics,
    verification_traces,
)
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor, VerificationRequired
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import TaskSnapshot, WorkflowInput
from app.tools.kubernetes import KubernetesStatus, ServiceRuntime
from app.tools.observability import LogsOutput, MetricsOutput, TracesOutput
from app.verifier.checks import (
    deployment_healthy,
    logs_healthy,
    metrics_healthy,
    pods_healthy,
    resources_healthy,
    traces_healthy,
)
from app.verifier.models import (
    VerificationCheck,
    VerificationConfig,
    VerificationReport,
    VerificationSpec,
    criteria_hash,
)
from app.verifier.scenario import sample_spec
from tests.test_tasks import loaded_task

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
SPEC = sample_spec(TaskSnapshot(str(uuid4()), TaskStatus.VERIFYING, 7))
CONFIG = VerificationConfig()


@pytest.mark.parametrize("change", ["names", "evidence"])
def test_report_cannot_claim_eight_checks_using_duplicates(change: str) -> None:
    names = (
        "deployment",
        "pods",
        "http_5xx_ratio",
        "http_p99_ms",
        "http_success_ratio",
        "logs",
        "traces",
        "resources",
    )
    shared = uuid4()
    checks = tuple(
        VerificationCheck(
            name="deployment" if change == "names" else name,
            passed=True,
            reason="测试报告",
            evidence_id=shared if change == "evidence" else uuid4(),
        )
        for name in names
    )
    with pytest.raises(ValidationError):
        VerificationReport(spec=SPEC, criteria_hash=criteria_hash(CONFIG), checks=checks)


def metrics(metric: str = "http_5xx_ratio", *, recovered: bool = True) -> MetricsOutput:
    return MetricsOutput(
        service_name=SPEC.service_name,
        start=SPEC.start,
        end=SPEC.end,
        series=tuple(
            s
            for s in verification_metrics(SPEC.start, SPEC.end, recovered=recovered)
            if s.metric_name == metric
        ),
    )


@pytest.mark.parametrize("metric", ["http_5xx_ratio", "http_p99_ms", "http_success_ratio"])
def test_entire_window_metrics_must_recover(metric: str) -> None:
    assert metrics_healthy(SPEC, CONFIG, metrics(metric), metric)
    assert not metrics_healthy(SPEC, CONFIG, metrics(metric, recovered=False), metric)


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "stale",
        "sparse",
        "wrong_service",
        "wrong_metric",
        "one_bad_point",
        "duplicate",
        "negative",
    ],
)
def test_missing_or_conflicting_metrics_do_not_resolve(change: str) -> None:
    data = metrics().model_dump(mode="json")
    if change == "empty":
        data["series"] = []
    elif change == "stale":
        data["series"][0]["points"] = data["series"][0]["points"][:3]
    elif change == "sparse":
        data["series"][0]["points"] = data["series"][0]["points"][::3]
    elif change in {"wrong_service", "wrong_metric"}:
        data["series"][0]["service_name" if change == "wrong_service" else "metric_name"] = "other"
    elif change == "duplicate":
        data["series"][0]["points"].append(data["series"][0]["points"][0])
    else:
        data["series"][0]["points"][0]["value"] = 0.9 if change == "one_bad_point" else -0.1
    assert not metrics_healthy(
        SPEC, CONFIG, MetricsOutput.model_validate_json(json.dumps(data)), "http_5xx_ratio"
    )


@pytest.mark.parametrize("recovered", [True, False])
def test_kubernetes_target_and_all_ready_replicas(recovered: bool) -> None:
    snapshot = verification_snapshot(recovered=recovered)
    deployment = KubernetesStatus(
        cluster_name=SPEC.cluster_name, namespace=SPEC.namespace, deployments=snapshot.deployments
    )
    pods = ServiceRuntime(
        cluster_name=SPEC.cluster_name,
        namespace=SPEC.namespace,
        service_name=SPEC.service_name,
        pods=snapshot.pods,
    )
    assert deployment_healthy(SPEC, deployment) is recovered
    assert pods_healthy(SPEC, pods) is recovered
    assert not deployment_healthy(SPEC.model_copy(update={"cluster_name": "other"}), deployment)
    assert not pods_healthy(SPEC.model_copy(update={"expected_image": "wrong:v1"}), pods)


@pytest.mark.parametrize("change", ["missing", "generation", "replicas", "condition"])
def test_deployment_missing_or_stale_is_unhealthy(change: str) -> None:
    snapshot = verification_snapshot()
    data = KubernetesStatus(
        cluster_name=SPEC.cluster_name, namespace=SPEC.namespace, deployments=snapshot.deployments
    ).model_dump(mode="json")
    if change == "missing":
        data["deployments"] = []
    elif change == "generation":
        data["deployments"][0]["status"]["observedGeneration"] = 6
    elif change == "replicas":
        data["deployments"][0]["status"]["updatedReplicas"] = 2
    else:
        data["deployments"][0]["status"]["conditions"][0]["status"] = "False"
    assert not deployment_healthy(SPEC, KubernetesStatus.model_validate_json(json.dumps(data)))


def test_error_logs_and_error_or_empty_traces_prevent_recovery() -> None:
    for recovered in (True, False):
        logs = LogsOutput(
            service_name=SPEC.service_name,
            start=SPEC.start,
            end=SPEC.end,
            logs=verification_logs(SPEC.start, recovered=recovered),
        )
        traces = TracesOutput(
            service_name=SPEC.service_name,
            start=SPEC.start,
            end=SPEC.end,
            traces=verification_traces(SPEC.start, recovered=recovered),
            topology=(),
        )
        assert logs_healthy(SPEC, logs) is recovered
        assert traces_healthy(SPEC, CONFIG, traces) is recovered
    assert not traces_healthy(SPEC, CONFIG, traces.model_copy(update={"traces": ()}))
    # 成功查询没有错误日志可以正常通过；缺少业务请求 Trace 不能通过。
    assert logs_healthy(SPEC, logs.model_copy(update={"logs": ()}))


@pytest.mark.parametrize(
    "change", ["healthy", "missing", "status", "no_sample", "stale", "saturated"]
)
def test_required_resource_status_and_connection_headroom(change: str) -> None:
    resource = next(r for r in sample_resources() if r.product == "rds")
    sample = RDSConnections(
        availability="available",
        max_connections=600,
        sampled_at=SPEC.end - timedelta(seconds=60),
        active_connections=60.0,
        total_connections=80.0,
    )
    if change == "no_sample":
        sample = RDSConnections(availability="no_data", max_connections=600)
    if change == "stale":
        sample = sample.model_copy(update={"sampled_at": SPEC.start})
    if change == "saturated":
        sample = sample.model_copy(update={"total_connections": 580.0})
    resource = resource.model_copy(
        update={"rds_connections": sample, "status": "Stopped" if change == "status" else "Running"}
    )
    data = CloudResources(
        service_name=SPEC.service_name,
        start=SPEC.start,
        end=SPEC.end,
        collected_at=SPEC.end,
        resources=() if change == "missing" else (resource,),
        events=(),
    )
    assert resources_healthy(SPEC, CONFIG, data) is (change == "healthy")


@pytest.mark.parametrize(
    "change", ["naive", "pre_action", "empty_resources", "extra", "zero_replicas"]
)
def test_verification_spec_rejects_invalid_targets(change: str) -> None:
    data = SPEC.model_dump(mode="json")
    if change == "naive":
        data["start"] = "2026-10-01T01:00:00"
    elif change == "pre_action":
        data["action_completed_at"] = SPEC.end.isoformat()
    elif change == "empty_resources":
        data["resources"] = []
    elif change == "extra":
        data["passed"] = True
    else:
        data["expected_replicas"] = 0
    with pytest.raises(ValidationError):
        VerificationSpec.model_validate_json(json.dumps(data))


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [TransitionActor.WORKFLOW, TransitionActor.VERIFIER])
async def test_actor_enum_cannot_grant_resolution_authority(
    actor: TransitionActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = True
    task = loaded_task(TaskStatus.VERIFYING, 7)
    session.scalar.return_value = task
    monkeypatch.setattr("app.tasks.takeover.takeover_record", AsyncMock(return_value=None))
    with pytest.raises(VerificationRequired):
        await TaskService(session).transition(
            task.id,
            TaskStatus.RESOLVED,
            expected_status=task.status,
            expected_version=7,
            reason="伪造恢复",
            actor=actor,
        )
    assert task.status is TaskStatus.VERIFYING and session.add.call_count == 0


def test_environment_config_and_same_task_workflow_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERIFICATION_CONFIG", '{"max_p99_ms":300.0}')
    assert Settings(APP_ENV="test").verification_config.max_p99_ms == 300.0
    validate_workflow_input(
        WorkflowInput(str(SPEC.task_id), verification_json=SPEC.model_dump_json())
    )
    with pytest.raises(ValueError):
        validate_workflow_input(
            WorkflowInput(str(uuid4()), verification_json=SPEC.model_dump_json())
        )
    with pytest.raises(ValueError):
        validate_workflow_input(
            WorkflowInput(
                str(SPEC.task_id),
                waits=[TaskStatus.WAITING_APPROVAL],
                verification_json=SPEC.model_dump_json(),
            )
        )
