import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.main import create_app, main
from app.config import Settings


def test_health_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_required_environment_prevents_startup() -> None:
    with pytest.raises(ValidationError) as error:
        create_app()
    assert error.value.errors()[0]["loc"] == ("APP_ENV",)


def test_invalid_environment_prevents_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "invalid")
    with pytest.raises(ValidationError, match="APP_ENV"):
        create_app()


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("API_HOST", "127.0.0.2")
    monkeypatch.setenv("API_PORT", "8100")
    settings = Settings()
    assert settings.app_env == "local"
    assert settings.api_host == "127.0.0.2"
    assert settings.api_port == 8100


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_invalid_port_prevents_startup(port: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("API_PORT", port)
    with pytest.raises(ValidationError, match="API_PORT"):
        create_app()


def test_api_entry_uses_validated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "8100")
    calls: list[tuple[str, int]] = []

    def fake_run(application: FastAPI, *, host: str, port: int) -> None:
        assert application.state.settings.app_env == "test"
        calls.append((host, port))

    monkeypatch.setattr("app.api.main.uvicorn.run", fake_run)
    main()
    assert calls == [("127.0.0.1", 8100)]
