"""只读验证的固定宿主目标；仅由本地样例显式调用。"""

from uuid import UUID

from app.connectors.observability.fake import SAMPLE_END, SAMPLE_START
from app.tasks.workflow_models import TaskSnapshot
from app.verifier.models import ResourceExpectation, VerificationSpec


def sample_spec(task: TaskSnapshot) -> VerificationSpec:
    return VerificationSpec(
        task_id=UUID(task.task_id),
        verifying_version=task.version,
        action_id="fake-rollback-payment",
        action_completed_at=SAMPLE_START,
        service_name="payment-service",
        start=SAMPLE_START,
        end=SAMPLE_END,
        cluster_name="ack-fake",
        namespace="payment",
        deployment_name="payment-service",
        container_name="payment",
        expected_image="registry.example.invalid/payment:v2.3.6",
        expected_replicas=3,
        resources=(
            ResourceExpectation(
                product="rds",
                region_id="cn-hangzhou",
                resource_id="rm-payment",
                healthy_status="Running",
            ),
        ),
    )
