"""离线 HTTP 契约：鉴权、输入校验、OpenAPI 与幂等命令身份。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError

from app.api.console import get_control_gateway, get_queries
from app.api.main import create_app
from app.auth.identity import Principal
from app.auth.service import AuthService
from app.config import Settings, parse_database_url
from app.db.session import Database
from app.tasks.console_models import AnswerInput, ApprovalInput, ControlCommand, TakeoverInput
from app.tasks.control import operation_id
from tests.auth_support import auth_config

pytestmark = pytest.mark.usefixtures("forbid_llm_network")

READ_PATHS = [
    "/api/tasks",
    "/api/tasks/{id}",
    "/api/tasks/{id}/interaction",
    "/api/events",
    "/api/events/{id}",
    "/api/evidence",
    "/api/evidence/{id}",
    "/api/tasks/{id}/evidence",
    "/api/tool-calls",
    "/api/tool-calls/{id}",
    "/api/tasks/{id}/tool-calls",
    "/api/tasks/{id}/status-history",
    "/api/status-history/{id}",
    "/api/incidents",
    "/api/incidents/{id}",
]
WRITE_PATHS = [
    f"/api/tasks/{{id}}/{kind}" for kind in ("approval", "judgment", "information", "takeover")
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", READ_PATHS + WRITE_PATHS)
async def test_every_console_route_requires_login(path: str) -> None:
    app = create_app(Settings(APP_ENV="test"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        result = await client.request(
            "POST" if path in WRITE_PATHS else "GET", path.format(id=uuid4()), json={}
        )
        assert result.status_code == 401


def authenticated_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, Database]:
    database = Database(parse_database_url("postgresql+asyncpg://127.0.0.1:1/unused"))
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=auth_config()))
    app.state.database = database
    principal = Principal("local-owner", uuid4(), datetime.now(UTC) + timedelta(hours=1), "csrf")

    async def authenticate(self: AuthService, cookie: str) -> Principal:
        return principal

    monkeypatch.setattr(AuthService, "authenticate", authenticate)
    app.dependency_overrides[get_queries] = object
    app.dependency_overrides[get_control_gateway] = object
    return app, database


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [p for p in READ_PATHS if "{id}" in p] + WRITE_PATHS)
async def test_every_identity_route_rejects_invalid_uuid(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, database = authenticated_app(monkeypatch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Cookie": "ops_session=test", "X-CSRF-Token": "csrf"},
        ) as client:
            response = await client.request(
                "POST" if path in WRITE_PATHS else "GET", path.format(id="bad-id"), json={}
            )
            assert response.status_code == 422
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [p for p in READ_PATHS if "{id}" not in p])
async def test_all_lists_bound_pagination(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    app, database = authenticated_app(monkeypatch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Cookie": "ops_session=test"},
        ) as client:
            for query in ("limit=0", "limit=101", "offset=-1", "limit=x"):
                assert (await client.get(path + "?" + query)).status_code == 422
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", WRITE_PATHS)
async def test_write_csrf_and_extra_actor_rejected(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, database = authenticated_app(monkeypatch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Cookie": "ops_session=test"},
        ) as client:
            target = path.format(id=uuid4())
            assert (await client.post(target, json={})).status_code == 403
            assert (
                await client.post(
                    target, json={"actor": "forged"}, headers={"X-CSRF-Token": "csrf"}
                )
            ).status_code == 422
    finally:
        await database.dispose()


def test_openapi_contains_all_paths_cookie_and_csrf() -> None:
    schema = create_app(Settings(APP_ENV="test")).openapi()
    paths = {
        path.replace("{task_id}", "{id}")
        .replace("{event_id}", "{id}")
        .replace("{evidence_id}", "{id}")
        .replace("{call_id}", "{id}")
        .replace("{history_id}", "{id}")
        .replace("{incident_id}", "{id}")
        for path in schema["paths"]
    }
    assert set(READ_PATHS + WRITE_PATHS) <= paths
    for path, methods in schema["paths"].items():
        if path.startswith("/api/") and not path.startswith("/api/auth/"):
            for method, operation in methods.items():
                assert operation["security"] == [{"SessionCookie": []}]
                assert {"404", "409", "503"} <= operation["responses"].keys()
                if method in {"post", "put", "delete"}:
                    assert any(
                        p["name"] == "X-CSRF-Token" and p["required"]
                        for p in operation["parameters"]
                    )


def test_command_identity_covers_actor_kind_version_and_payload() -> None:
    command = ControlCommand(
        task_id=uuid4(),
        actor="owner",
        kind="takeover",
        payload={"expected_version": 3, "reason": "先人工处理"},
    )
    assert operation_id(command) == operation_id(
        ControlCommand.model_validate_json(command.model_dump_json())
    )
    for change in (
        {"actor": "other"},
        {"kind": "judgment"},
        {"payload": {"expected_version": 4, "reason": "先人工处理"}},
    ):
        assert operation_id(command.model_copy(update=change)) != operation_id(command)


@pytest.mark.parametrize(
    "model,body",
    [
        (TakeoverInput, {"expected_version": True, "reason": "接管"}),
        (TakeoverInput, {"expected_version": 1, "reason": "  "}),
        (AnswerInput, {"question_id": str(uuid4()), "wait_version": True, "answer": "回答"}),
        (AnswerInput, {"question_id": str(uuid4()), "wait_version": 1, "answer": "  "}),
        (
            ApprovalInput,
            {
                "approval_id": str(uuid4()),
                "wait_version": 1,
                "action_hash": "0" * 64,
                "decision": "expired",
            },
        ),
    ],
)
def test_control_inputs_reject_ambiguous_values(
    model: type[BaseModel], body: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(body)
