"""聊天 HTTP/SSE 契约与离线失败边界；禁止真实网络。"""

import asyncio
import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.agent.chat.gateway import ChatGateway
from app.agent.chat.models import ChatAnswer, ChatInput
from app.api.chat import get_chat_gateway, stream
from app.api.main import create_app
from app.config import Settings
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import WorkflowInput
from app.triggers.schemas import EventReceipt
from tests.test_console import authenticated_app
from tests.test_main_agent import SPEC

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def body(**changes: object) -> dict[str, object]:
    return {
        "request_id": str(uuid4()),
        "service_name": "payment-service",
        "message": "支付服务 5xx 的原因是什么？",
        "start": SPEC.start.isoformat(),
        "end": SPEC.end.isoformat(),
        **changes,
    }


def parse_events(raw: str) -> list[tuple[str, dict[str, Any]]]:
    result = []
    for block in raw.split("\n\n"):
        if block.startswith("id:"):
            lines = block.splitlines()
            result.append((lines[1].removeprefix("event: "), json.loads(lines[2][6:])))
    return result


@pytest.mark.parametrize(
    "changes",
    [
        {"message": " "},
        {"message": "x" * 4001},
        {"message": 4},
        {"request_id": "bad"},
        {"service_name": "../../root"},
        {"service_name": ""},
        {"mode": "approve"},
        {"actor": "forged"},
        {"approved": True},
        {"execution_enabled": True},
        {"risk_level": "L0"},
        {"start": None},
        {"end": "2026-10-01T02:00:00"},
        {"end": SPEC.start.isoformat()},
        {"end": "2026-10-03T02:00:00Z"},
    ],
)
def test_reject_invalid_or_authorizing_input(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChatInput.model_validate_json(json.dumps(body(**changes)))


def test_input_utc_default_mode_and_frozen() -> None:
    value = ChatInput.model_validate_json(json.dumps(body(end="2026-10-01T10:00:00+08:00")))
    assert value.end == SPEC.end and value.mode == "question"
    with pytest.raises(ValidationError):
        value.message = "修改"  # type: ignore[misc]


def test_openapi_typed_sse_and_authentication() -> None:
    schema = create_app(Settings(APP_ENV="test")).openapi()
    operation = schema["paths"]["/api/chat"]["post"]
    assert "text/event-stream" in operation["responses"]["200"]["content"]
    assert operation["security"] == [{"SessionCookie": []}]
    assert any(p["name"] == "X-CSRF-Token" and p["required"] for p in operation["parameters"])
    assert "ChatAnswer" in schema["components"]["schemas"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("POST", "/api/chat"), ("GET", f"/api/chat/{uuid4()}")])
async def test_auth_required(method: str, path: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings(APP_ENV="test"))),
        base_url="http://127.0.0.1",
    ) as client:
        assert (await client.request(method, path, json=body())).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": "csrf", "Origin": "http://attacker.invalid"}],
)
async def test_csrf_and_origin(headers: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    api, db = authenticated_app(monkeypatch)
    api.dependency_overrides[get_chat_gateway] = object
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://127.0.0.1",
            headers={"Cookie": "ops_session=test", **headers},
        ) as client:
            assert (await client.post("/api/chat", json=body())).status_code == 403
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_sse_frames_citations_chunks_actor_and_no_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, db = authenticated_app(monkeypatch)
    task_id, fact = uuid4(), uuid4()
    receipt = EventReceipt(str(uuid4()), str(task_id), f"ai-task-{task_id}", False)
    gateway = ChatGateway(AsyncMock(), db, Settings(APP_ENV="test"))
    submit = AsyncMock(return_value=receipt)
    answer = ChatAnswer(
        task_id=task_id,
        status="CLOSED",
        pending=False,
        answer="核验回答 [Evidence:" + str(fact) + "]" + "。" * 260,
        evidence_ids=(fact,),
    )
    gateway.submit = submit  # type: ignore[method-assign]
    gateway.wait = AsyncMock(return_value=answer)  # type: ignore[method-assign]
    api.dependency_overrides[get_chat_gateway] = lambda: gateway
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://127.0.0.1",
            headers={"Cookie": "ops_session=test", "X-CSRF-Token": "csrf"},
        ) as client:
            response = await client.post("/api/chat", json=body())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        events = parse_events(response.text)
        assert events[0][0] == "task" and events[1][0] == "evidence" and events[-1][0] == "done"
        assert (
            "".join(str(payload["text"]) for kind, payload in events if kind == "delta")
            == answer.answer
        )
        assert len([e for e in events if e[0] == "delta"]) >= 3
        assert submit.call_args.args[0].actor == "local-owner"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_timeout_emits_safe_error_and_disconnect_keeps_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, db = authenticated_app(monkeypatch)
    gateway = ChatGateway(AsyncMock(), db, Settings(APP_ENV="test", CHAT_STREAM_TIMEOUT_SECONDS=1))
    receipt = EventReceipt(str(uuid4()), str(uuid4()), "ai-task-fake", False)
    canceled = asyncio.Event()

    async def wait(value: EventReceipt) -> ChatAnswer:
        try:
            await asyncio.Future[None]()
        finally:
            canceled.set()
        raise AssertionError("不可到达")

    monkeypatch.setattr(gateway, "wait", wait)
    try:
        raw = "".join([part async for part in stream(gateway, receipt)])
        assert [e[0] for e in parse_events(raw)] == ["task", "error"]
        assert canceled.is_set()
        generator = stream(gateway, receipt)
        await anext(generator)
        await generator.aclose()
        gateway.client.get_workflow_handle.assert_not_called()  # type: ignore[attr-defined]
    finally:
        await db.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"chat_mode": "approve"},
        {"execution_enabled": True},
        {"investigation_json": None},
        {"architecture_review": True},
        {"war_room": True},
        {"inspection_mode": "inspection"},
        {"ticket_id": "ticket"},
        {"release_id": "release"},
    ],
)
def test_chat_workflow_cannot_mix_execution_or_other_modes(changes: dict[str, object]) -> None:
    value = WorkflowInput(
        str(uuid4()), investigation_json=SPEC.model_dump_json(), chat_mode="question"
    )
    with pytest.raises(ValueError):
        validate_workflow_input(replace(value, **changes))  # type: ignore[arg-type]
