"""隔离验收的 Fake 资源与已人工审核保障 Runbook。"""

from uuid import UUID, uuid4

from app.config import Settings
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector, fake_binding
from app.db.session import Database
from app.executor.models import ExecutionBinding
from app.policy.models import RiskLevel
from app.runbooks.embedding import embedding_client
from app.runbooks.lifecycle import RunbookLifecycle
from app.runbooks.maturity import MaturityConfig, content_hash
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import DiagnosticStep, RunbookCondition
from app.runbooks.service import RunbookService
from app.tasks.service import TaskService
from app.tasks.states import TaskSource


def fake_resources(
    service: str = "payment-service",
) -> tuple[FakeKubernetesWriteConnector, ExecutionBinding]:
    resource = FakeKubernetesWriteConnector()
    binding = fake_binding().model_copy(
        update={"service_name": service, "deployment_name": service}
    )
    original = resource.targets["payment-service"]
    resource.targets = {
        service: original.model_copy(update={"service_name": service, "deployment_name": service})
    }
    return resource, binding


async def seed_runbook(database: Database, service: str) -> UUID:
    settings = Settings(APP_ENV="test")
    draft = payment_runbook("war-room-" + uuid4().hex).model_copy(
        update={
            "description": "重大保障容量、监控和回滚检查",
            "applicability_conditions": (
                RunbookCondition(field="service_name", operator="equals", value=service),
                RunbookCondition(field="title", operator="contains", value="重大保障"),
            ),
            "diagnostic_steps": (
                DiagnosticStep(
                    description="读取服务关联上下文",
                    tool_name="get_service_context",
                    parameters={"service_name": "$service_name"},
                    risk_level=RiskLevel.L0,
                ),
            ),
        }
    )
    async with database.session() as session, session.begin():
        service_layer = RunbookService(session, lambda request: embedding_client(settings, request))
        runbook = await service_layer.create(draft)
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="Fake 保障 Runbook 人工审核", reason="验收样例"
        )
        reviewed = await RunbookLifecycle(session, MaturityConfig()).review(
            runbook.id,
            task_id=task.id,
            request_id=uuid4(),
            expected_revision=content_hash(runbook),
            actor="fake-owner",
            approved=True,
        )
        return reviewed.id
