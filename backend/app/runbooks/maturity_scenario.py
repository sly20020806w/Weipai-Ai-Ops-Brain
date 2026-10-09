"""仅用于本机隔离 Fake 演示：通过真实匹配和独立验证准备试用任务。"""

from uuid import UUID, uuid4

from app.agent.investigation import InvestigationSpec
from app.config import Settings
from app.db.session import Database
from app.runbooks.activities import RunbookActivities
from app.runbooks.embedding import embedding_client
from app.runbooks.lifecycle import RunbookLifecycle
from app.runbooks.maturity import content_hash
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import AutomationLevel, RunbookMaturity, RunbookView
from app.runbooks.service import RunbookService
from app.runbooks.workflow_models import RunbookMatchRequest
from app.tasks.activities import TaskActivityStore
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest
from app.verifier.scenario import sample_spec


async def create_sample(database: Database, settings: Settings) -> RunbookView:
    if settings.app_env not in {"local", "test"} or settings.connector_mode.value != "fake":
        raise ValueError("成熟度演示只允许本机 Fake")
    async with database.session() as session, session.begin():
        return await RunbookService(
            session, lambda request: embedding_client(settings, request)
        ).create(
            payment_runbook(f"payment-maturity-{uuid4().hex}").model_copy(
                update={
                    "maturity": RunbookMaturity.DRAFT,
                    "automation_level": AutomationLevel.MANUAL,
                    "confidence": 0.5,
                }
            )
        )


async def human_review(database: Database, settings: Settings, guide: RunbookView) -> RunbookView:
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="人工审核支付 Runbook", reason="Step 35 Fake 验收"
        )
        return await RunbookLifecycle(session, settings.runbook_maturity_config).review(
            guide.id,
            task_id=task.id,
            request_id=uuid4(),
            expected_revision=content_hash(guide),
            actor="local-owner",
            approved=True,
        )


async def prepare_trial(
    database: Database,
    settings: Settings,
    runbook_id: UUID,
    *,
    until: TaskStatus = TaskStatus.VERIFYING,
) -> TaskSnapshot:
    if settings.app_env not in {"local", "test"} or settings.connector_mode.value != "fake":
        raise ValueError("试用任务只允许本机 Fake")
    async with database.session() as session, session.begin():
        task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="payment-service 5xx 试用", reason="Step 35 Fake 试用"
        )
        snapshot = TaskSnapshot(str(task.id), task.status, task.status_version)
    store = TaskActivityStore(database)
    for status in (TaskStatus.CONTEXT_BUILDING, TaskStatus.RUNBOOK_MATCHING):
        snapshot = await store.transition(TransitionRequest(snapshot, status, "Fake 试用前置阶段"))
    window = sample_spec(TaskSnapshot(snapshot.task_id, TaskStatus.VERIFYING, 6))
    spec = InvestigationSpec(
        title=task.title, service_name=window.service_name, start=window.start, end=window.end
    )
    match = await RunbookActivities(database, settings).match(
        RunbookMatchRequest(snapshot, spec.model_dump_json())
    )
    if (
        not match.runbook_json
        or RunbookView.model_validate_json(match.runbook_json).id != runbook_id
    ):
        raise ValueError("试用未匹配到指定 Runbook")
    for status in (
        TaskStatus.INVESTIGATING,
        TaskStatus.RCA,
        TaskStatus.PLANNING,
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
    ):
        snapshot = await store.transition(
            TransitionRequest(snapshot, status, "Fake 无写入试用前置阶段")
        )
        if status is until:
            return snapshot
    return snapshot
