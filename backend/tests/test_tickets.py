"""工单离线契约测试，禁止实际网络。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.agent.models import ChatMessage, ChatRequest
from app.config import Settings
from app.connectors.ops_platform.tickets import FakeTicketReader, FakeTicketWriter, TicketState
from app.db.base import utc_now
from app.executor.ticket_models import PermissionGrant, TicketCommand
from app.tasks.tickets.demo import demo_settings
from app.tasks.tickets.models import Classification, TicketCategory
from app.tasks.tickets.scenario import classify_response
from app.tasks.tickets.service import allowed_request
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import WorkflowInput

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("forbid_llm_network")]


def command(state: TicketState) -> TicketCommand:
    ticket = state.tickets["TICKET-PERMISSION"]
    assert ticket.permission_request is not None
    return TicketCommand(
        execution_id=uuid4(),
        task_id=uuid4(),
        plan_evidence_id=uuid4(),
        plan_hash="a" * 64,
        action_id="permission",
        name="grant_ticket_permission",
        service_name=ticket.service_name,
        ticket_id=ticket.id,
        expected_updated_at=ticket.updated_at,
        grant=PermissionGrant.model_validate_json(ticket.permission_request.model_dump_json()),
    )


async def test_reader_write_separation_and_fake_snapshot() -> None:
    reader = FakeTicketReader(TicketState())
    assert not hasattr(reader, "execute") and not hasattr(reader, "issue")
    assert (await reader.get_ticket("TICKET-INCOMPLETE")).status == "open"
    await reader.aclose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("ticket_id", "TICKET-INCOMPLETE"),
        ("service_name", "checkout-service"),
        ("action_id", "different"),
        ("plan_hash", "b" * 64),
        ("execution_id", uuid4()),
    ],
)
async def test_action_credential_cannot_operate_other_target(field: str, value: object) -> None:
    state = TicketState()
    writer = FakeTicketWriter(state)
    original = command(state)
    credential = await writer.issue(original, 60)
    with pytest.raises(PermissionError):
        await writer.execute(original.model_copy(update={field: value}), credential)
    assert state.execution_count == 0


async def test_expired_or_forged_credentials_and_idempotency() -> None:
    state = TicketState()
    now = utc_now()
    writer = FakeTicketWriter(state, clock=lambda: now)
    original = command(state)
    credential = await writer.issue(original, 60)
    forged = credential.model_copy(update={"token": "forged"})
    with pytest.raises((PermissionError, ValueError)):
        await writer.execute(original, forged)
    first = await writer.execute(original, credential)
    assert await writer.execute(original, credential) == first and state.execution_count == 1
    now += timedelta(seconds=60)
    with pytest.raises(PermissionError):
        await writer.execute(original, credential)


@pytest.mark.parametrize(
    "change",
    [
        {"subject_id": "other"},
        {"resource": "database"},
        {"permission": "admin"},
        {"expires_at": utc_now() + timedelta(days=8)},
        {"expires_at": utc_now() - timedelta(seconds=1)},
    ],
)
async def test_permissions_require_exact_host_binding(change: dict[str, object]) -> None:
    grant = command(TicketState()).grant.model_copy(update=change)
    assert not allowed_request(
        demo_settings("unused"), "payment-service", "requester-sample", grant
    )


async def test_config_default_disabled_and_no_fake_permission_binding() -> None:
    settings = Settings(APP_ENV="test")
    assert not settings.ticket_config.enabled and not settings.ticket_config.bindings


async def test_ticket_input_does_not_mix_other_scenarios() -> None:
    with pytest.raises(ValueError):
        validate_workflow_input(
            WorkflowInput(str(uuid4()), ticket_id="ticket", investigation_json="{}")
        )
    validate_workflow_input(WorkflowInput(str(uuid4()), ticket_id="ticket"))


@pytest.mark.parametrize(
    "title,category",
    [
        ("SQL申请", TicketCategory.SQL),
        ("权限申请", TicketCategory.PERMISSION),
        ("资源申请", TicketCategory.RESOURCE),
        ("配置变更", TicketCategory.CONFIGURATION),
        ("业务咨询", TicketCategory.CONSULTATION),
        ("故障调查", TicketCategory.INCIDENT),
        ("发布申请", TicketCategory.RELEASE),
    ],
)
async def test_seven_ticket_classifications_cite_source(
    title: str, category: TicketCategory
) -> None:
    evidence_id = uuid4()
    request = ChatRequest(
        messages=(
            ChatMessage(role="system", content="分类"),
            ChatMessage(
                role="user",
                content=json.dumps({"ticket": {"title": title}, "evidence_id": str(evidence_id)}),
            ),
        )
    )
    response = classify_response(request)
    classified = Classification.model_validate_json(response.message.content or "{}")
    assert classified.category is category and classified.rationale.evidence_ids == (evidence_id,)
