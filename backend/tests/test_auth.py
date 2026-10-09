"""离线鉴权：缺省关闭、配置校验、密码脱敏与整个 API 门禁。"""

import json
from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.api.main import create_app
from app.auth.config import AuthConfig
from app.auth.passwords import hash_password, verify_password
from app.auth.service import AuthService
from app.config import Settings, parse_database_url
from app.db.session import Database
from tests.auth_support import PASSWORD, PASSWORD_HASH, auth_config

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def test_salted_password_verification_and_no_plaintext() -> None:
    second = hash_password(PASSWORD)
    assert second != PASSWORD_HASH and PASSWORD not in second
    assert verify_password(PASSWORD, second)
    assert not verify_password("错误密码", second)
    with pytest.raises(ValueError):
        hash_password("short")


@pytest.mark.parametrize(
    "field,value",
    [
        ("username", " owner "),
        ("username", ""),
        ("password_hash", "plaintext-password"),
        ("session_secret", "short"),
        ("public_origin", "http://remote.example.invalid"),
        ("public_origin", "https://example.invalid/path"),
        ("public_origin", "https://user:password@example.invalid"),
        ("public_origin", "https://example.invalid:bad"),
        ("public_origin", "https://example.invalid?"),
        ("public_origin", "https://example.invalid#"),
        ("public_origin", "https://@example.invalid"),
        ("public_origin", "https://example.invalid\x00"),
        ("session_ttl_seconds", True),
        ("session_ttl_seconds", 0),
        ("max_login_failures", 0),
        ("login_lock_seconds", 0),
    ],
)
def test_invalid_auth_config_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        auth_config(**{field: value})


def test_config_from_environment_and_secret_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    config = auth_config()
    raw = config.model_dump(mode="json")
    raw["password_hash"] = PASSWORD_HASH
    raw["session_secret"] = config.session_secret.get_secret_value()
    monkeypatch.setenv("AUTH_CONFIG", json.dumps(raw))
    settings = Settings(APP_ENV="test")
    assert settings.auth_config == config
    assert PASSWORD_HASH not in repr(settings) and PASSWORD_HASH not in settings.model_dump_json()
    assert config.session_secret.get_secret_value() not in str(config)
    with pytest.raises(ValidationError) as error:
        AuthConfig.model_validate({**raw, "session_secret": "sensitive-bad-key"})
    assert "sensitive-bad-key" not in str(error.value)


def test_origin_canonicalization_matches_browser_serialization() -> None:
    assert (
        auth_config(public_origin="HTTPS://OPS.EXAMPLE.INVALID:443/").public_origin
        == "https://ops.example.invalid"
    )
    assert auth_config(public_origin="http://[::1]:80/").public_origin == "http://[::1]"


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_cloud_api_requires_auth_https_and_database(environment: str) -> None:
    with pytest.raises(ValueError, match="AUTH_CONFIG"):
        create_app(Settings(APP_ENV=environment))
    with pytest.raises(ValueError, match="HTTPS"):
        create_app(Settings(APP_ENV=environment, AUTH_CONFIG=auth_config()))
    with pytest.raises(ValueError, match="DATABASE_URL"):
        create_app(
            Settings(
                APP_ENV=environment,
                AUTH_CONFIG=auth_config(public_origin="https://ops.example.invalid"),
            )
        )
    # Worker/迁移读取 Settings 不需要 API 账户。
    assert Settings(APP_ENV=environment).auth_config is None


@pytest.mark.parametrize("path", ["/api", "/api/auth/me", "/api/tasks", "/api/missing"])
@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "OPTIONS"])
@pytest.mark.asyncio
async def test_all_api_paths_require_session(path: str, method: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings(APP_ENV="test"))),
        base_url="http://127.0.0.1",
    ) as client:
        assert (await client.request(method, path)).status_code == 401
        assert (await client.get("/health")).status_code == 200
        assert (await client.post("/webhooks/prometheus", json={})).status_code == 401


@pytest.mark.asyncio
async def test_local_unconfigured_login_fails_closed() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings(APP_ENV="test"))),
        base_url="http://127.0.0.1",
    ) as client:
        assert (await client.post("/api/auth/login", json={})).status_code == 503
        assert (await client.get("/api/auth/login")).status_code == 401
        assert (await client.get("/api/auth/login/")).status_code == 401


@pytest.mark.asyncio
async def test_invalid_cookies_rejected_before_database() -> None:
    database = Database(parse_database_url("postgresql+asyncpg://127.0.0.1:1/unused"))
    service = AuthService(database, auth_config())
    try:
        for token in ("", "x", f"{uuid4().hex}.{'0' * 64}", "../x", "a" * 10000):
            assert await service.authenticate(token) is None
        session_id = uuid4()
        first = service.cookie_for(session_id)
        assert first != AuthService(database, auth_config(username="other")).cookie_for(session_id)
        assert first != AuthService(database, auth_config(session_secret="b" * 64)).cookie_for(
            session_id
        )
        assert first != AuthService(
            database, auth_config(password_hash=hash_password("rotated-test-password"))
        ).cookie_for(session_id)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_login_origin_header_and_validation_redaction() -> None:
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=auth_config()))
    database = Database(parse_database_url("postgresql+asyncpg://127.0.0.1:1/unused"))
    app.state.database = database
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.post("/api/auth/login", json={})).status_code == 403
        for extra in (
            {"Origin": "https://evil.example.invalid"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert (
                await client.post("/api/auth/login", json={}, headers={"X-Ops-Login": "1", **extra})
            ).status_code == 403
        response = await client.post(
            "/api/auth/login",
            headers={"X-Ops-Login": "1"},
            json={"username": 123, "password": "secret-to-redact", "actor": "forged-secret"},
        )
        assert response.status_code == 422
        assert "secret-to-redact" not in response.text and "forged-secret" not in response.text
        assert all("input" not in item for item in response.json()["detail"])
        assert response.headers["cache-control"] == "no-store"
    await database.dispose()


def test_openapi_declares_configured_cookie_and_public_login() -> None:
    schema = create_app(
        Settings(
            APP_ENV="test", AUTH_CONFIG=auth_config(public_origin="https://ops.example.invalid")
        )
    ).openapi()
    assert schema["components"]["securitySchemes"]["SessionCookie"]["name"] == "__Host-ops_session"
    for path in ("/api/auth/me", "/api/auth/logout"):
        operation = next(iter(schema["paths"][path].values()))
        assert operation["security"] == [{"SessionCookie": []}]
    assert "security" not in schema["paths"]["/api/auth/login"]["post"]
    for path, header in (("/api/auth/login", "X-Ops-Login"), ("/api/auth/logout", "X-CSRF-Token")):
        assert any(
            item["name"] == header and item["required"]
            for item in schema["paths"][path]["post"]["parameters"]
        )


@pytest.mark.asyncio
async def test_session_store_unavailable_fails_closed_and_redacts_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.auth.identity import Principal

    database = Database(parse_database_url("postgresql+asyncpg://127.0.0.1:1/unused"))
    app = create_app(Settings(APP_ENV="test", AUTH_CONFIG=auth_config()))
    app.state.database = database

    async def unavailable(self: AuthService, cookie: str) -> Principal | None:
        raise OSError("private-database-password")

    monkeypatch.setattr(AuthService, "authenticate", unavailable)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get(
                "/api/auth/me", headers={"Cookie": "ops_session=unavailable"}
            )
            assert response.status_code == 503
            assert "private-database-password" not in response.text
    finally:
        await database.dispose()
