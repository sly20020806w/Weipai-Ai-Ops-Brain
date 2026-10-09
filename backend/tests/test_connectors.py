"""Step 10：离线验证配置选型、读写凭证边界和生命周期。"""

from abc import abstractmethod
from collections.abc import Callable
from typing import cast

import httpx2 as httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.api.main import create_app
from app.config import Settings
from app.connectors.base import Connector, ReadOnlyConnector, WriteConnector
from app.connectors.factory import ConnectorFactory
from app.connectors.models import (
    ConnectorMode,
    ExecutorCredentials,
    ReaderCredentials,
    validate_credential_separation,
)

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


@pytest.fixture(autouse=True)
def forbid_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("框架测试禁止构建真实 HTTP 客户端")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", blocked)
    monkeypatch.setattr(httpx.Client, "__init__", blocked)


class SampleReader(ReadOnlyConnector):
    """测试专用接口；实际业务 Connector 留给 Step 11–16。"""

    @abstractmethod
    async def read_status(self) -> str: ...

    async def aclose(self) -> None:
        self.closed = True


class FakeSampleReader(SampleReader):
    async def read_status(self) -> str:
        return "fake"


class RealSampleReader(SampleReader):
    """真实分支的构造替身：不包含 API 请求或运维业务。"""

    async def read_status(self) -> str:
        return "real-constructor-stub"


class SampleWriter(WriteConnector):
    async def aclose(self) -> None:
        pass


def sample_factory() -> ConnectorFactory[SampleReader]:
    return ConnectorFactory[SampleReader]("sample", fake=FakeSampleReader, real=RealSampleReader)


@pytest.mark.parametrize("environment", ["local", "test", "staging", "production"])
@pytest.mark.asyncio
async def test_default_fake_has_no_credentials_and_same_reader_interface(environment: str) -> None:
    settings = Settings(APP_ENV=environment, CONNECTOR_READER_TOKENS={"sample": "reader-test"})
    reader = sample_factory().create(settings)
    assert isinstance(reader, FakeSampleReader)
    assert settings.connector_mode is ConnectorMode.FAKE
    assert reader.reader_credentials is None
    async with reader as opened:
        assert opened is reader
        assert await opened.read_status() == "fake"
    assert reader.closed
    assert not isinstance(reader, WriteConnector)
    assert not hasattr(reader, "write")
    assert not hasattr(reader, "execute_action")
    assert not hasattr(reader, "_executor_credentials")


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.asyncio
async def test_real_configuration_selects_real_constructor_only(environment: str) -> None:
    def forbidden_fake() -> SampleReader:
        raise AssertionError("真实配置不能调用 Fake 工厂")

    factory = ConnectorFactory[SampleReader]("sample", fake=forbidden_fake, real=RealSampleReader)
    settings = Settings(
        APP_ENV=environment,
        CONNECTOR_MODE="real",
        CONNECTOR_READER_TOKENS={"sample": "reader-test"},
    )
    reader = factory.create(settings)
    assert isinstance(reader, RealSampleReader)
    assert reader.reader_credentials == ReaderCredentials(connector="sample", token="reader-test")
    assert await reader.read_status() == "real-constructor-stub"
    await reader.aclose()
    assert reader.closed


def test_fake_never_constructs_real_even_if_credentials_are_available() -> None:
    def forbidden_real(credentials: ReaderCredentials) -> SampleReader:
        raise AssertionError("Fake 配置不能调用真实工厂")

    factory = ConnectorFactory[SampleReader]("sample", fake=FakeSampleReader, real=forbidden_real)
    assert isinstance(
        factory.create(Settings(APP_ENV="test", CONNECTOR_READER_TOKENS={"sample": "reader-test"})),
        FakeSampleReader,
    )


@pytest.mark.parametrize("environment", ["local", "test"])
def test_local_real_rejected_at_startup(environment: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("CONNECTOR_MODE", "real")
    with pytest.raises(ValidationError, match="只允许 fake"):
        create_app()


@pytest.mark.parametrize("mode", ["real", ConnectorMode.REAL])
def test_factory_revalidates_copied_config_before_any_constructor(mode: str) -> None:
    settings = Settings(APP_ENV="test").model_copy(update={"connector_mode": mode})
    with pytest.raises(ValidationError, match="只允许 fake"):
        sample_factory().create(settings)


def test_factory_revalidates_mutated_config() -> None:
    settings = Settings(APP_ENV="local")
    settings.connector_mode = ConnectorMode.REAL
    with pytest.raises(ValidationError, match="只允许 fake"):
        sample_factory().create(settings)


def test_environment_config_and_secrets_are_not_displayed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("CONNECTOR_MODE", "real")
    monkeypatch.setenv("CONNECTOR_READER_TOKENS", '{"sample":"reader-test-secret"}')
    settings = Settings()
    reader = sample_factory().create(settings)
    assert isinstance(reader, RealSampleReader)
    assert "reader-test-secret" not in repr(settings)
    assert "reader-test-secret" not in settings.model_dump_json()
    assert "reader-test-secret" not in repr(reader.reader_credentials)


@pytest.mark.parametrize("token_map", [{}, {"other": "other-test"}])
def test_missing_real_reader_token_fails_without_falling_back(token_map: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="CONNECTOR_READER_TOKENS.*sample"):
        sample_factory().create(
            Settings(APP_ENV="staging", CONNECTOR_MODE="real", CONNECTOR_READER_TOKENS=token_map)
        )


@pytest.mark.parametrize("mode", ["invalid", "REAL", "", None])
def test_invalid_mode_is_rejected(mode: object) -> None:
    with pytest.raises(ValidationError, match="CONNECTOR_MODE"):
        Settings(APP_ENV="test", CONNECTOR_MODE=mode)


@pytest.mark.parametrize("name", ["", "bad-name", "中文", "A", "a" * 65])
def test_invalid_connector_names(name: str) -> None:
    with pytest.raises(ValueError, match="英文标识符"):
        ConnectorFactory[SampleReader](name, fake=FakeSampleReader, real=RealSampleReader)
    with pytest.raises(ValidationError):
        Settings(APP_ENV="test", CONNECTOR_READER_TOKENS={name: "reader-test"})


@pytest.mark.parametrize("token", ["", " ", "\n"])
def test_empty_reader_credentials_rejected(token: str) -> None:
    with pytest.raises(ValidationError, match="凭证不能为空"):
        Settings(APP_ENV="test", CONNECTOR_READER_TOKENS={"sample": token})


def test_read_write_identity_separation_and_masking() -> None:
    reader = ReaderCredentials(connector="sample", token="reader-test-secret")
    executor = ExecutorCredentials(connector="sample", token="executor-test-secret")
    validate_credential_separation(reader, executor)
    assert not isinstance(reader, ExecutorCredentials)
    assert not isinstance(executor, ReaderCredentials)
    assert "reader-test-secret" not in reader.model_dump_json()
    assert "executor-test-secret" not in repr(executor)
    with pytest.raises(ValidationError, match="frozen"):
        reader.token = SecretStr("changed")  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        executor.token = SecretStr("changed")  # type: ignore[misc]
    with pytest.raises(ValueError, match="不能复用"):
        validate_credential_separation(
            reader, ExecutorCredentials(connector="sample", token="reader-test-secret")
        )
    with pytest.raises(ValueError, match="同一个"):
        validate_credential_separation(
            reader, ExecutorCredentials(connector="other", token="executor-test-secret")
        )


def test_swapped_credential_types_cannot_cross_connector_boundary() -> None:
    reader = ReaderCredentials(connector="sample", token="reader-test")
    executor = ExecutorCredentials(connector="sample", token="executor-test")
    with pytest.raises(TypeError, match="只接受 ReaderCredentials"):
        FakeSampleReader(cast(ReaderCredentials, executor))
    with pytest.raises(TypeError, match="只接受 ExecutorCredentials"):
        SampleWriter(cast(ExecutorCredentials, reader))
    with pytest.raises(TypeError, match="读写凭证类型"):
        validate_credential_separation(cast(ReaderCredentials, executor), executor)
    assert isinstance(SampleWriter(executor), WriteConnector)


@pytest.mark.asyncio
async def test_context_manager_closes_after_exception() -> None:
    reader = FakeSampleReader()
    with pytest.raises(RuntimeError, match="模拟失败"):
        async with reader:
            raise RuntimeError("模拟失败")
    assert reader.closed


def test_bases_cannot_instantiate_without_lifecycle_implementation() -> None:
    for base in (Connector, ReadOnlyConnector, WriteConnector):
        with pytest.raises(TypeError, match="abstract"):
            base()  # type: ignore[abstract, call-arg]


def test_miswired_fake_and_real_factories_fail_explicitly() -> None:
    def fake_with_token() -> SampleReader:
        return FakeSampleReader(ReaderCredentials(connector="sample", token="reader-test"))

    bad_fake = ConnectorFactory[SampleReader]("sample", fake=fake_with_token, real=RealSampleReader)
    with pytest.raises(TypeError, match="不携带真实凭证"):
        bad_fake.create(Settings(APP_ENV="test"))
    bad_real = ConnectorFactory[SampleReader](
        "sample", fake=FakeSampleReader, real=lambda credentials: FakeSampleReader()
    )
    with pytest.raises(TypeError, match="绑定该 Reader"):
        bad_real.create(
            Settings(
                APP_ENV="staging",
                CONNECTOR_MODE="real",
                CONNECTOR_READER_TOKENS={"sample": "reader-test"},
            )
        )
    writer_factory = cast(
        Callable[[], SampleReader],
        lambda: SampleWriter(ExecutorCredentials(connector="sample", token="executor-test")),
    )
    with pytest.raises(TypeError, match="只读 Connector"):
        ConnectorFactory[SampleReader]("sample", fake=writer_factory, real=RealSampleReader).create(
            Settings(APP_ENV="test")
        )
