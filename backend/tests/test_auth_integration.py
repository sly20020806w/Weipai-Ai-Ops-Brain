"""本机 PostgreSQL：真实 HTTP 会话、撤销、并发限速与人工审计归因。"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from typing import Annotated
from uuid import UUID

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import Depends, Request
from sqlalchemy import delete, inspect, select

from app.api.auth import require_principal
from app.api.main import create_app
from app.auth.models import AuthSession, LoginGuard
from app.auth.passwords import hash_password
from app.auth.service import AuthService, LoginRejected
from app.config import Settings, parse_database_url
from app.connectors.feishu.fake import FakeFeishuConnector
from app.db.session import Database
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.tasks.approval.service import ApprovalStore
from app.tasks.human.activities import HumanInteractionStore
from app.tasks.states import TaskStatus
from app.tasks.workflow_models import (
    ApprovalDecisionRequest,
    HumanAnswerRequest,
)
from tests.auth_support import PASSWORD, auth_config
from tests.database_support import get_test_database_url, migrate

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="执行 check-auth.ps1"),
]


@pytest.fixture(scope="module")
def migrated_schema() -> None:
    migrate("upgrade", "head")


@pytest_asyncio.fixture
async def database(migrated_schema: None) -> AsyncIterator[Database]:
    instance = Database(parse_database_url(get_test_database_url()))
    try:
        async with instance.session() as session, session.begin():
            await session.execute(delete(AuthSession))
            await session.execute(delete(LoginGuard))
        yield instance
    finally:
        await instance.dispose()


@pytest_asyncio.fixture
async def client(database: Database) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=auth_config()))
    app.state.database = database
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as instance:
        yield instance


async def login(client: httpx.AsyncClient) -> httpx.Response:
    response = await client.post(
        "/api/auth/login",
        headers={"X-Ops-Login": "1"},
        json={"username": "local-owner", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response


async def test_cookie_login_namespace_logout_and_stolen_cookie_rejected(
    client: httpx.AsyncClient, database: Database
) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401
    response = await login(client)
    cookie = client.cookies["ops_session"]
    csrf = response.json()["csrf_token"]
    assert response.json()["actor"] == "local-owner"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    assert "password" not in response.text and "session_secret" not in response.text
    assert (await client.get("/api/auth/me")).status_code == 200
    # 新增业务路由继续受 namespace 会话门禁保护。
    assert (await client.get("/api/tasks")).status_code == 200
    assert (await client.post("/api/auth/logout")).status_code == 403
    for headers in (
        {"X-CSRF-Token": "invalid"},
        {"X-CSRF-Token": csrf, "Origin": "https://evil.example.invalid"},
        {"X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"},
    ):
        assert (await client.post("/api/auth/logout", headers=headers)).status_code == 403
    result = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert result.status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (
        await client.get("/api/auth/me", headers={"Cookie": f"ops_session={cookie}"})
    ).status_code == 401
    async with database.session() as session:
        rows = list(await session.scalars(select(AuthSession)))
        assert len(rows) == 1 and rows[0].revoked_at is not None
        assert rows[0].created_at.tzinfo is UTC and rows[0].expires_at.tzinfo is UTC
        assert rows[0].revoked_at.tzinfo is UTC


async def test_new_service_and_api_restart_preserve_session(
    client: httpx.AsyncClient, database: Database
) -> None:
    await login(client)
    cookie = client.cookies["ops_session"]
    service = AuthService(database, auth_config())
    principal = await service.authenticate(cookie)
    assert principal is not None and principal.actor == "local-owner"
    other = Database(parse_database_url(get_test_database_url()))
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=auth_config()))
    try:
        app.state.database = other
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Cookie": f"ops_session={cookie}"},
        ) as restarted:
            assert (await restarted.get("/api/auth/me")).status_code == 200
    finally:
        await other.dispose()


async def test_server_expiry_boundary_and_future_session_rejected(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AuthService(database, auth_config())
    issued = await service.login("local-owner", PASSWORD)
    monkeypatch.setattr("app.auth.service.utc_now", lambda: issued.principal.expires_at)
    assert await service.authenticate(issued.cookie) is None
    monkeypatch.setattr(
        "app.auth.service.utc_now", lambda: issued.principal.expires_at - timedelta(hours=2)
    )
    assert await service.authenticate(issued.cookie) is None


@pytest.mark.parametrize("change", ["key", "password", "username", "origin"])
async def test_config_rotation_invalidates_old_session(database: Database, change: str) -> None:
    issued = await AuthService(database, auth_config()).login("local-owner", PASSWORD)
    overrides = {
        "key": {"session_secret": "b" * 64},
        "password": {"password_hash": hash_password("rotated-test-password")},
        "username": {"username": "new-owner"},
        "origin": {"public_origin": "https://ops.example.invalid"},
    }
    assert (
        await AuthService(database, auth_config(**overrides[change])).authenticate(issued.cookie)
        is None
    )


async def test_parallel_bad_logins_are_bounded_and_lock_survives_restart(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = auth_config(max_login_failures=2, login_lock_seconds=10)
    service = AuthService(database, config)
    results = await asyncio.gather(
        *(service.login("wrong-user", "wrong-password") for _ in range(5)), return_exceptions=True
    )
    assert all(isinstance(item, LoginRejected) for item in results)
    assert sum(isinstance(item, LoginRejected) and item.locked for item in results) == 3
    restarted = AuthService(database, config)
    with pytest.raises(LoginRejected) as error:
        await restarted.login("local-owner", PASSWORD)
    assert error.value.locked
    async with database.session() as session:
        guard = await session.scalar(select(LoginGuard))
        assert guard is not None and guard.locked_until is not None
        later = guard.locked_until + timedelta(seconds=1)
    monkeypatch.setattr("app.auth.service.utc_now", lambda: later)
    assert (await restarted.login("local-owner", PASSWORD)).principal.actor == "local-owner"


async def test_login_401_429_and_invalid_body_do_not_leak_secrets(
    client: httpx.AsyncClient, database: Database
) -> None:
    for username in ("nonexistent-owner", "local-owner"):
        response = await client.post(
            "/api/auth/login",
            headers={"X-Ops-Login": "1"},
            json={"username": username, "password": "wrong-secret"},
        )
        assert response.status_code == 401 and response.json() == {"detail": "用户名或密码错误"}
    for _ in range(3):
        await client.post(
            "/api/auth/login",
            headers={"X-Ops-Login": "1"},
            json={"username": "local-owner", "password": "wrong-secret"},
        )
    response = await client.post(
        "/api/auth/login",
        headers={"X-Ops-Login": "1"},
        json={"username": "local-owner", "password": PASSWORD},
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
    async with database.session() as session:
        assert list(await session.scalars(select(AuthSession))) == []


async def test_session_database_has_no_password_cookie_csrf_or_config(database: Database) -> None:
    issued = await AuthService(database, auth_config()).login("local-owner", PASSWORD)
    async with database.engine.connect() as connection:
        columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("auth_sessions"))
        assert {row["name"] for row in columns} == {
            "id",
            "created_at",
            "updated_at",
            "actor",
            "expires_at",
            "revoked_at",
        }
    async with database.session() as session:
        row = await session.get(AuthSession, issued.principal.session_id)
        assert row is not None
        assert PASSWORD not in str(row.__dict__) and issued.cookie not in str(row.__dict__)
        assert issued.principal.csrf_token not in str(row.__dict__)


async def test_https_cookie_has_host_secure_flags(database: Database) -> None:
    config = auth_config(public_origin="https://ops.example.invalid")
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=config))
    app.state.database = database
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=config.public_origin
    ) as http:
        response = await login(http)
        header = response.headers["set-cookie"]
        assert header.startswith("__Host-ops_session=") and "Secure" in header
        assert "Path=/" in header and "Domain=" not in header
        assert (await http.get("/api/auth/me")).status_code == 200


async def test_authenticated_actor_reaches_real_approval_audit(database: Database) -> None:
    from tests.test_approval_integration import waiting
    from tests.test_main_agent_integration import seed

    await seed(database)
    request, _ = await waiting(database)
    store = ApprovalStore(database, Settings(APP_ENV="test"))
    prompt = await store.notify(request, FakeFeishuConnector())
    issued = await AuthService(database, auth_config()).login("local-owner", PASSWORD)
    principal = await AuthService(database, auth_config()).authenticate(issued.cookie)
    assert principal is not None
    result = await store.decide(
        ApprovalDecisionRequest(prompt, principal.approval_response(prompt, "approved"))
    )
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(request.task.task_id))
        audit = next(item for item in audits if str(item.evidence_id) == result.evidence_id)
        assert audit.event_type is AuditEventType.APPROVAL and audit.actor == principal.actor
        assert audit.occurred_at.tzinfo is UTC


async def test_identity_dependency_ignores_client_actor(database: Database) -> None:
    from app.auth.identity import Principal

    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=auth_config()))
    app.state.database = database

    @app.post("/api/identity-probe")
    async def probe(
        request: Request, principal: Annotated[Principal, Depends(require_principal)]
    ) -> dict[str, str]:
        return {"actor": principal.actor}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as http:
        signed_in = await login(http)
        result = await http.post(
            "/api/identity-probe?actor=forged",
            json={"actor": "forged"},
            headers={"X-CSRF-Token": signed_in.json()["csrf_token"], "X-Actor": "forged"},
        )
        assert result.status_code == 200 and result.json() == {"actor": "local-owner"}
        # 有效身份也不能跳过签名 Webhook。
        assert (await http.post("/webhooks/prometheus", json={})).status_code == 401


async def test_authenticated_actor_reaches_human_judgment_audit(database: Database) -> None:
    from tests.test_human_interaction_integration import waiting

    wait_request = await waiting(database, TaskStatus.NEED_HUMAN_JUDGMENT)
    store = HumanInteractionStore(database)
    prompt = await store.notify(wait_request, FakeFeishuConnector())
    issued = await AuthService(database, auth_config()).login("local-owner", PASSWORD)
    # 回答契约不接收额外 actor，身份由已验证会话派生。
    request = HumanAnswerRequest(prompt, issued.principal.human_answer(prompt, "先保障支付业务"))
    result = await store.answer(request)
    async with database.session() as session:
        audits = await LedgerService(session).audits_for_task(UUID(wait_request.task.task_id))
        audit = next(item for item in audits if str(item.evidence_id) == result.answer_evidence_id)
        assert audit.event_type is AuditEventType.HUMAN_INTERACTION
        assert audit.actor == "local-owner" and audit.occurred_at.tzinfo is UTC
