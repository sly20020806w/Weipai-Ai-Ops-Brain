"""隔离本机 PostgreSQL / Temporal 的工单闭环与安全门禁。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import parse_database_url
from app.connectors.kubernetes.execution import ActionCredential
from app.connectors.ops_platform.tickets import FakeTicketWriter, TicketState
from app.db.base import utc_now
from app.db.session import Database
from app.executor.ticket_models import TicketCommand, TicketReceipt
from app.ledger.service import LedgerService
from app.runbooks.embedding import embedding_client
from app.runbooks.lifecycle import RunbookLifecycle
from app.runbooks.maturity import content_hash
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import RunbookDraft
from app.runbooks.service import RunbookService
from app.tasks.models import AITask
from app.tasks.planning.models import ActionPlan
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.tickets.activities import TicketActivities
from app.tasks.tickets.demo import approve, demo_settings, push_ticket, wait_progress
from app.tasks.tickets.models import TicketExecutionRequest
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import HumanAnswer, TaskSnapshot, WorkflowProgress
from app.triggers.activities import EventActivities
from app.triggers.models import OpsEvent
from tests.database_support import get_test_database_url, migrate

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("TEST_TEMPORAL_ADDRESS"),
        reason="执行 check-tickets.ps1",
    ),
]


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        yield instance
    finally:
        await instance.dispose()


async def client() -> Client:
    return await Client.connect(
        os.environ["TEST_TEMPORAL_ADDRESS"],
        namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default"),
    )


async def test_ticket_closed_with_evidence_learning_replay_and_duplicate_event(
    database: Database,
) -> None:
    temporal = await client()
    settings = demo_settings("ticket-test-" + uuid4().hex)
    state = TicketState()
    tickets = TicketActivities(database, settings, state=state)
    receipt = await push_ticket(database, state)
    async with create_worker(temporal, database, settings, ticket_activities=tickets):
        events = EventActivities(database, settings, temporal)
        await events.start_task(receipt)
        await events.start_task(receipt)
        handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
        progress = await wait_progress(handle, TaskStatus.WAITING_APPROVAL)
        assert state.issue_count == 0 and state.execution_count == 0
        assert progress.action_plan_json and '"L4"' in progress.action_plan_json
        await approve(handle)
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is TaskStatus.CLOSED
        assert state.execution_count == 2
        ticket = next(t for t in state.tickets.values() if t.id.startswith("ticket-"))
        assert ticket.status == "closed" and ticket.resolution and "Evidence:" in ticket.resolution
        async with database.session() as session:
            task = await session.get(AITask, UUID(receipt.task_id))
            assert task and task.source is TaskSource.TICKET
            evidence = await LedgerService(session).evidence_for_task(task.id)
            assert all(
                any(str(e.id) == reference for e in evidence)
                for reference in ticket.resolution.split("Evidence: ")[1].split(", ")
            )
            assert any(e.source_tool == "ticket.learning" for e in evidence)
            audits = await LedgerService(session).audits_for_task(task.id)
            assert (
                sum(a.operation == "execute_action" and a.outcome == "succeeded" for a in audits)
                == 2
            )
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
        assert state.execution_count == 2
        assert progress.action_plan_json and progress.action_plan_evidence_id
        plan = ActionPlan.model_validate_json(progress.action_plan_json)
        request = TicketExecutionRequest(
            TaskSnapshot(receipt.task_id, TaskStatus.EXECUTING, plan.planning_version + 2),
            progress.action_plan_evidence_id,
            progress.approval_prompt,
            0,
        )
        committed = await asyncio.gather(*(tickets.executor.execute(request) for _ in range(3)))
        assert len(set(committed)) == 1 and state.execution_count == 2
        await tickets.executor.replay(request, UUID(committed[0]))
        assert state.execution_count == 2


async def test_missing_information_waits_then_answer_resumes(database: Database) -> None:
    temporal = await client()
    settings = demo_settings("ticket-information-" + uuid4().hex)
    state = TicketState()
    receipt = await push_ticket(database, state, complete=False)
    async with create_worker(
        temporal,
        database,
        settings,
        ticket_activities=TicketActivities(database, settings, state=state),
    ):
        await EventActivities(database, settings, temporal).start_task(receipt)
        handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
        progress = await wait_progress(handle, TaskStatus.WAITING_INFORMATION)
        assert state.execution_count == 0 and progress.human_prompt
        prompt = progress.human_prompt
        answer = json.dumps(
            {
                "resource": "payment-logs",
                "permission": "read",
                "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
                "reason": "排查支付工单",
            }
        )
        await handle.signal(
            AITaskWorkflow.answer_question,
            HumanAnswer(
                prompt.question_id, prompt.task.status, prompt.task.version, answer, "test-operator"
            ),
        )
        await approve(handle)
        result = await asyncio.wait_for(handle.result(), 60)
        assert (
            result.task
            and result.task.status is TaskStatus.CLOSED
            and len(result.human_answers) == 1
        )
        assert state.execution_count == 2


@pytest.mark.parametrize("decision", ["rejected", "timeout"])
async def test_rejection_timeout_never_writes(database: Database, decision: str) -> None:
    temporal = await client()
    settings = demo_settings(
        "ticket-rejected-" + uuid4().hex, timeout=1 if decision == "timeout" else 30
    )
    state = TicketState()
    receipt = await push_ticket(database, state)
    async with create_worker(
        temporal,
        database,
        settings,
        ticket_activities=TicketActivities(database, settings, state=state),
    ):
        await EventActivities(database, settings, temporal).start_task(receipt)
        handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
        if decision == "rejected":
            await approve(handle, decision=decision)
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is TaskStatus.ESCALATED
        assert state.issue_count == state.execution_count == 0


class IncorrectPermissionWriter(FakeTicketWriter):
    async def execute(self, command: TicketCommand, credential: ActionCredential) -> TicketReceipt:
        receipt = await super().execute(command, credential)
        if command.name == "grant_ticket_permission":
            self.state.grants.clear()
        return receipt


async def test_independent_verifier_blocks_closure_when_permission_not_restored(
    database: Database,
) -> None:
    temporal = await client()
    settings = demo_settings("ticket-verifier-" + uuid4().hex)
    state = TicketState()
    tickets = TicketActivities(database, settings, state=state)
    tickets.executor.writer = IncorrectPermissionWriter(state)
    receipt = await push_ticket(database, state)
    async with create_worker(temporal, database, settings, ticket_activities=tickets):
        await EventActivities(database, settings, temporal).start_task(receipt)
        handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
        await approve(handle)
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is TaskStatus.INVESTIGATING
        assert state.execution_count == 1
        assert all(t.status == "open" for t in state.tickets.values() if t.id.startswith("ticket-"))


async def test_permission_binding_changed_after_approval_zero_credentials(
    database: Database,
) -> None:
    temporal = await client()
    settings = demo_settings("ticket-binding-" + uuid4().hex)
    state = TicketState()
    tickets = TicketActivities(database, settings, state=state)
    receipt = await push_ticket(database, state)
    async with create_worker(temporal, database, settings, ticket_activities=tickets):
        await EventActivities(database, settings, temporal).start_task(receipt)
        handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
        await wait_progress(handle, TaskStatus.WAITING_APPROVAL)
        tickets.executor.settings = settings.model_copy(
            update={"ticket_config": settings.ticket_config.model_copy(update={"bindings": ()})}
        )
        await approve(handle)
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is TaskStatus.ESCALATED
        assert state.issue_count == state.execution_count == 0


@pytest.mark.parametrize("recovered", [True, False])
async def test_reviewed_runbook_counts_independent_ticket_outcome_once(
    database: Database, recovered: bool
) -> None:
    temporal = await client()
    settings = demo_settings("ticket-runbook-" + uuid4().hex)
    state = TicketState()
    tickets = TicketActivities(database, settings, state=state)
    if not recovered:
        tickets.executor.writer = IncorrectPermissionWriter(state)
    receipt = await push_ticket(database, state)
    async with database.session() as session, session.begin():
        event = await session.get(OpsEvent, UUID(receipt.event_id))
        assert event is not None
        data = payment_runbook().model_dump(mode="json")
        data.update(
            name="permission-" + uuid4().hex,
            risk_level="L4",
            applicability_conditions=[
                {"field": "title", "operator": "contains", "value": event.external_id}
            ],
            exclusion_conditions=[],
            diagnostic_steps=[
                {
                    "description": "读取当前用户资源权限",
                    "tool_name": "query_ticket_permission",
                    "parameters": {
                        "service_name": "$service_name",
                        "subject_id": "user-sample",
                        "resource": "payment-logs",
                    },
                    "risk_level": "L0",
                }
            ],
        )
        guide = await RunbookService(
            session, lambda request: embedding_client(settings, request)
        ).create(RunbookDraft.model_validate_json(json.dumps(data)))
        review_task = await TaskService(session).create(
            source=TaskSource.HUMAN, title="审核权限 Runbook", reason="人工审核验收"
        )
        await RunbookLifecycle(session).review(
            guide.id,
            task_id=review_task.id,
            request_id=uuid4(),
            expected_revision=content_hash(guide),
            actor="test-operator",
            approved=True,
        )
    async with create_worker(temporal, database, settings, ticket_activities=tickets):
        await EventActivities(database, settings, temporal).start_task(receipt)
        handle = temporal.get_workflow_handle(receipt.workflow_id, result_type=WorkflowProgress)
        await approve(handle)
        result = await asyncio.wait_for(handle.result(), 60)
        assert result.task and result.task.status is (
            TaskStatus.CLOSED if recovered else TaskStatus.INVESTIGATING
        )
        await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
    async with database.session() as session:
        current = await RunbookService(
            session, lambda request: embedding_client(settings, request)
        ).get(guide.id)
        assert current.success_count == int(recovered) and current.failure_count == int(
            not recovered
        )
