"""Step 11：共享只读契约与配置 HTTP 协议；全部使用 Fake/MockTransport。"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import httpx2 as httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.config import Settings
from app.connectors.base import WriteConnector
from app.connectors.models import ExecutorCredentials, ReaderCredentials
from app.connectors.ops_platform.__main__ import main as demo
from app.connectors.ops_platform.client import (
    HTTPOpsPlatformConnector,
    OpsPlatformConnector,
    OpsPlatformError,
    OpsPlatformHTTPError,
    OpsPlatformNotFound,
    OpsPlatformResponseError,
    OpsPlatformTimeout,
    OpsPlatformTransportError,
)
from app.connectors.ops_platform.config import OpsPlatformConfig
from app.connectors.ops_platform.factory import create_ops_platform_connector
from app.connectors.ops_platform.fake import FakeOpsPlatformConnector, sample_snapshot
from app.connectors.ops_platform.models import OpsPlatformSnapshot, SourceRecord, Ticket

pytestmark = pytest.mark.usefixtures("forbid_llm_network")

CONFIG = {
    "base_url": "https://ops.example.invalid/company/api/v1",
    "service_tree_path": "cmdb/tree",
    "applications_path": "cmdb/apps",
    "owners_path": "cmdb/owners",
    "tickets_path": "ops/tickets",
    "timeout_seconds": 1.25,
    "page_size": 1,
    "max_pages": 10,
}
READER = ReaderCredentials(connector="ops_platform", token="reader-mock-secret")


def real_settings(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "APP_ENV": "staging",
            "CONNECTOR_MODE": "real",
            "OPS_PLATFORM_CONFIG": CONFIG,
            "CONNECTOR_READER_TOKENS": {"ops_platform": READER.token},
            **overrides,
        }
    )


def mock_source(snapshot: OpsPlatformSnapshot) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "ops.example.invalid"
        assert request.headers["authorization"] == "Bearer reader-mock-secret"
        assert request.headers["accept"] == "application/json"
        assert request.extensions["timeout"] == dict(connect=1.25, read=1.25, write=1.25, pool=1.25)
        query = request.url.params
        path = request.url.path.removeprefix("/company/api/v1/")
        records: tuple[SourceRecord, ...]
        if path == "cmdb/tree":
            records = snapshot.service_tree
        elif path == "cmdb/apps":
            records = tuple(
                app
                for app in snapshot.applications
                if ("business_id" not in query or app.business_id == query["business_id"])
                and ("service_name" not in query or app.service_name == query["service_name"])
            )
        elif path == "cmdb/owners":
            ids = {
                owner_id
                for app in snapshot.applications
                if app.service_name == query["service_name"]
                for owner_id in app.owner_ids
            }
            records = tuple(owner for owner in snapshot.owners if owner.id in ids)
        elif path == "ops/tickets":
            records = tuple(
                ticket
                for ticket in snapshot.tickets
                if ("ticket_id" not in query or ticket.id == query["ticket_id"])
                and ("service_name" not in query or ticket.service_name == query["service_name"])
                and ("status" not in query or ticket.status == query["status"])
            )
        else:
            raise AssertionError(f"未声明的读取路径：{path}")
        start = int(query.get("cursor", "0"))
        limit = int(query["limit"])
        page = records[start : start + limit]
        cursor = str(start + limit) if start + limit < len(records) else None
        return httpx.Response(
            200,
            json={
                "items": [record.model_dump(mode="json") for record in page],
                "next_cursor": cursor,
            },
        )

    return httpx.MockTransport(handle)


@pytest_asyncio.fixture(params=["fake", "http-mock"])
async def reader(request: pytest.FixtureRequest) -> AsyncIterator[OpsPlatformConnector]:
    connector = (
        create_ops_platform_connector(Settings(APP_ENV="test"))
        if request.param == "fake"
        else create_ops_platform_connector(
            real_settings(), transport=mock_source(sample_snapshot())
        )
    )
    async with connector:
        yield connector


@pytest.mark.asyncio
async def test_shared_contract_payment_business_owners_and_tickets(
    reader: OpsPlatformConnector,
) -> None:
    app = await reader.get_application("payment-service")
    nodes = {node.id: node for node in await reader.list_service_tree()}
    assert app.business_id == "payment"
    assert nodes[app.business_id].name == "支付业务（样例）"
    assert nodes[app.business_id].parent_id == "weipai"
    owners = await reader.list_owners(app.service_name)
    assert tuple(owner.id for owner in owners) == app.owner_ids == ("owner-payment",)
    assert owners[0].team == "支付团队（样例）"
    assert await reader.list_applications(business_id="payment") == (app,)
    assert len(await reader.list_applications()) == 2
    tickets = await reader.list_tickets(service_name=app.service_name, status="open")
    assert len(tickets) == 1 and tickets[0].id == "TICKET-1001"
    assert await reader.get_ticket(tickets[0].id) == tickets[0]
    assert tickets[0].created_at.tzinfo is UTC and tickets[0].updated_at.tzinfo is UTC
    assert len(await reader.list_tickets()) == 2
    assert len(await reader.list_tickets(status="closed")) == 1


@pytest.mark.asyncio
async def test_shared_contract_unknown_records_and_empty_filters(
    reader: OpsPlatformConnector,
) -> None:
    with pytest.raises(OpsPlatformNotFound):
        await reader.get_application("missing-service")
    with pytest.raises(OpsPlatformNotFound):
        await reader.get_ticket("missing-ticket")
    assert await reader.list_owners("missing-service") == ()
    assert await reader.list_applications(business_id="missing") == ()
    assert await reader.list_tickets(service_name="missing-service") == ()
    assert await reader.list_tickets(service_name="payment-service", status="closed") == ()


@pytest.mark.parametrize("invalid", ["", " ", "payment-service\n", "a" * 257])
@pytest.mark.asyncio
async def test_shared_contract_rejects_invalid_identifiers(
    reader: OpsPlatformConnector, invalid: str
) -> None:
    with pytest.raises(ValidationError):
        await reader.get_application(invalid)
    with pytest.raises(ValidationError):
        await reader.list_applications(business_id=invalid)
    with pytest.raises(ValidationError):
        await reader.list_owners(invalid)
    with pytest.raises(ValidationError):
        await reader.get_ticket(invalid)
    with pytest.raises(ValidationError):
        await reader.list_tickets(status=invalid)


@pytest.mark.asyncio
async def test_shared_contract_read_only_lifecycle(reader: OpsPlatformConnector) -> None:
    assert not isinstance(reader, WriteConnector)
    for method in (
        "write",
        "post",
        "delete",
        "execute_action",
        "close_ticket",
        "update_application",
    ):
        assert not hasattr(reader, method)
    await reader.aclose()
    await reader.aclose()
    with pytest.raises(OpsPlatformError, match="已关闭"):
        await reader.list_service_tree()


@pytest.mark.parametrize("environment", ["local", "test", "staging", "production"])
@pytest.mark.asyncio
async def test_default_factory_fake_never_builds_http(
    environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Fake 不得构建 HTTP 客户端")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", blocked)
    async with create_ops_platform_connector(
        Settings(
            APP_ENV=environment, CONNECTOR_READER_TOKENS={"ops_platform": "unused-real-secret"}
        )
    ) as reader:
        assert isinstance(reader, FakeOpsPlatformConnector)
        assert reader.reader_credentials is None
        assert (await reader.get_application("payment-service")).business_id == "payment"


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.asyncio
async def test_real_factory_uses_same_interface_without_construction_request(
    environment: str,
) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    async with create_ops_platform_connector(
        real_settings(APP_ENV=environment), transport=httpx.MockTransport(handle)
    ) as reader:
        assert isinstance(reader, HTTPOpsPlatformConnector)
        assert isinstance(reader, OpsPlatformConnector)
        assert reader.reader_credentials == READER
        assert calls == []
        assert await reader.list_service_tree() == ()
    assert len(calls) == 1


@pytest.mark.parametrize("environment", ["local", "test"])
def test_modified_config_cannot_bypass_fake_gate(environment: str) -> None:
    settings = Settings(APP_ENV=environment).model_copy(update={"connector_mode": "real"})
    with pytest.raises(ValidationError, match="只允许 fake"):
        create_ops_platform_connector(settings)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"OPS_PLATFORM_CONFIG": None}, "OPS_PLATFORM_CONFIG"),
        ({"CONNECTOR_READER_TOKENS": {}}, "ops_platform"),
    ],
)
def test_real_factory_requires_explicit_configuration_and_reader(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        create_ops_platform_connector(real_settings(**overrides))


def test_env_config_loaded_and_reader_secret_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("CONNECTOR_MODE", "real")
    monkeypatch.setenv("OPS_PLATFORM_CONFIG", json.dumps(CONFIG))
    monkeypatch.setenv("CONNECTOR_READER_TOKENS", '{"ops_platform":"reader-mock-secret"}')
    settings = Settings()
    assert settings.require_ops_platform_config().base_url.endswith("/company/api/v1/")
    assert "reader-mock-secret" not in repr(settings)
    assert "reader-mock-secret" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://ops.example.invalid",
        "https://user:secret@ops.example.invalid",
        "file:///tmp",
        "https://ops.example.invalid/?token=secret",
        "https://ops.example.invalid/#fragment",
        "https://ops.example.invalid:0",
        "https://ops.example.invalid:bad",
        "https://ops.example.invalid/a/../b",
        "https://ops.example.invalid/%2e%2e/b",
        "https://ops.example.invalid\\other",
        "https://ops.example.invalid/ bad",
    ],
)
def test_invalid_base_url_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        OpsPlatformConfig(**{**CONFIG, "base_url": url})


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/apps",
        "../apps",
        "https://other.invalid/apps",
        "//other",
        "apps?token=secret",
        "apps#x",
        "apps/%2e%2e",
        "apps\\other",
    ],
)
def test_endpoint_cannot_escape_configured_origin(path: str) -> None:
    for field in ("service_tree_path", "applications_path", "owners_path", "tickets_path"):
        with pytest.raises(ValidationError, match="相对路径"):
            OpsPlatformConfig(**{**CONFIG, field: path})


@pytest.mark.parametrize(
    "field, value",
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", 121),
        ("page_size", 0),
        ("page_size", 501),
        ("max_pages", 0),
        ("max_pages", 1001),
    ],
)
def test_invalid_http_limits_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        OpsPlatformConfig(**{**CONFIG, field: value})


def test_reader_credentials_wrong_identity_and_type_rejected() -> None:
    config = OpsPlatformConfig(**CONFIG)
    with pytest.raises(TypeError, match="ReaderCredentials"):
        HTTPOpsPlatformConnector(
            config,
            cast(
                ReaderCredentials,
                ExecutorCredentials(connector="ops_platform", token="executor-mock"),
            ),
        )
    with pytest.raises(ValueError, match="ops_platform"):
        HTTPOpsPlatformConnector(config, ReaderCredentials(connector="other", token="other-mock"))
    for token in ("secret with space", "secret\n"):
        with pytest.raises(ValueError, match="控制字符"):
            HTTPOpsPlatformConnector(
                config, ReaderCredentials(connector="ops_platform", token=token)
            )
    with pytest.raises(ValidationError):
        HTTPOpsPlatformConnector(
            config.model_copy(update={"base_url": "https://bad/?secret=x"}), READER
        )


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500, 503])
@pytest.mark.asyncio
async def test_http_errors_and_redirects_do_not_follow_or_leak(status: int) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status,
            text="reader-mock-secret source-sensitive-body",
            headers={"Location": "https://other.example.invalid/leak"},
        )

    async with create_ops_platform_connector(
        real_settings(), transport=httpx.MockTransport(handle)
    ) as reader:
        with pytest.raises(OpsPlatformHTTPError) as error:
            await reader.list_service_tree()
        assert error.value.status_code == status
        assert "reader-mock-secret" not in str(error.value)
        assert "source-sensitive-body" not in str(error.value)
        assert error.value.__context__ is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_404_normalized_as_not_found() -> None:
    async with create_ops_platform_connector(
        real_settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, text="reader-mock-secret")
        ),
    ) as reader:
        with pytest.raises(OpsPlatformNotFound, match="不存在"):
            await reader.get_ticket("missing")


@pytest.mark.parametrize("kind", ["timeout", "connection"])
@pytest.mark.asyncio
async def test_transport_failure_sanitized_without_local_retry(kind: str) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        exception = httpx.ReadTimeout if kind == "timeout" else httpx.ConnectError
        raise exception("reader-mock-secret source-sensitive-body", request=request)

    expected = OpsPlatformTimeout if kind == "timeout" else OpsPlatformTransportError
    async with create_ops_platform_connector(
        real_settings(), transport=httpx.MockTransport(handle)
    ) as reader:
        with pytest.raises(expected) as error:
            await reader.list_tickets()
        assert "reader-mock-secret" not in str(error.value)
        assert error.value.__suppress_context__
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not-json reader-mock-secret",
        b"{}",
        b'{"items":[{"id":"secret"}]}',
        b'{"items":null}',
        b'{"items":[],"next_cursor":""}',
    ],
)
@pytest.mark.asyncio
async def test_malformed_response_rejected_without_leak(body: bytes) -> None:
    async with create_ops_platform_connector(
        real_settings(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    ) as reader:
        with pytest.raises(OpsPlatformResponseError) as error:
            await reader.list_service_tree()
        assert "reader-mock-secret" not in str(error.value)


@pytest.mark.parametrize(
    "kind, max_pages, expected_calls",
    [
        ("cursor-repeat", 10, 2),
        ("duplicate-id", 10, 2),
        ("page-limit", 1, 1),
    ],
)
@pytest.mark.asyncio
async def test_bad_pagination_cannot_return_partial_results(
    kind: str, max_pages: int, expected_calls: int
) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        record_id = "same" if kind == "duplicate-id" else f"node-{len(calls)}"
        cursor = "same" if kind == "cursor-repeat" else str(len(calls))
        return httpx.Response(
            200, json={"items": [{"id": record_id, "name": "样例"}], "next_cursor": cursor}
        )

    settings = real_settings(OPS_PLATFORM_CONFIG={**CONFIG, "max_pages": max_pages})
    async with create_ops_platform_connector(
        settings, transport=httpx.MockTransport(handle)
    ) as reader:
        with pytest.raises(OpsPlatformResponseError):
            await reader.list_service_tree()
    assert len(calls) == expected_calls


@pytest.mark.asyncio
async def test_pagination_keeps_filters_and_cursor_is_never_a_url() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.params["service_name"] == "payment-service"
        if len(calls) == 1:
            return httpx.Response(
                200, json={"items": [], "next_cursor": "https://other.example.invalid/?x=secret"}
            )
        assert request.url.params["cursor"] == "https://other.example.invalid/?x=secret"
        assert request.url.host == "ops.example.invalid"
        return httpx.Response(
            200, json={"items": [sample_snapshot().tickets[0].model_dump(mode="json")]}
        )

    async with create_ops_platform_connector(
        real_settings(), transport=httpx.MockTransport(handle)
    ) as reader:
        assert len(await reader.list_tickets(service_name="payment-service")) == 1
    assert len(calls) == 2


@pytest.mark.parametrize(
    "operation", ["application", "business", "ticket-id", "ticket-service", "ticket-status"]
)
@pytest.mark.asyncio
async def test_response_target_mismatch_rejected(operation: str) -> None:
    records = sample_snapshot()
    item = (
        records.applications[1] if operation in {"application", "business"} else records.tickets[1]
    )
    async with create_ops_platform_connector(
        real_settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": [item.model_dump(mode="json")]})
        ),
    ) as reader:
        with pytest.raises(OpsPlatformResponseError):
            if operation == "application":
                await reader.get_application("payment-service")
            elif operation == "business":
                await reader.list_applications(business_id="payment")
            elif operation == "ticket-id":
                await reader.get_ticket("TICKET-1001")
            elif operation == "ticket-service":
                await reader.list_tickets(service_name="payment-service")
            else:
                await reader.list_tickets(status="open")


@pytest.mark.asyncio
async def test_query_data_cannot_inject_additional_parameters() -> None:
    service = "payment-service&cursor=evil?token=other"

    def handle(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"service_name": service, "limit": "1"}
        return httpx.Response(200, json={"items": []})

    async with create_ops_platform_connector(
        real_settings(), transport=httpx.MockTransport(handle)
    ) as reader:
        assert await reader.list_tickets(service_name=service) == ()


def test_ticket_times_normalize_utc_and_reject_naive_or_reversed_time() -> None:
    body = sample_snapshot().tickets[0].model_dump(mode="json")
    body["created_at"] = "2026-10-01T09:00:00+08:00"
    parsed = Ticket.model_validate_json(json.dumps(body))
    assert parsed.created_at == datetime(2026, 10, 1, 1, tzinfo=UTC)
    assert parsed.created_at.tzinfo is UTC
    body["created_at"] = "2026-10-01T01:00:00"
    with pytest.raises(ValidationError):
        Ticket.model_validate_json(json.dumps(body))
    body["created_at"] = "2026-10-01T02:00:00Z"
    with pytest.raises(ValidationError, match="不能早于"):
        Ticket.model_validate_json(json.dumps(body))


@pytest.mark.asyncio
async def test_fake_snapshot_can_be_injected_and_is_immutable() -> None:
    snapshot = sample_snapshot()
    app = snapshot.applications[0].model_copy(update={"name": "自定义样例"})
    customized = snapshot.model_copy(update={"applications": (app, snapshot.applications[1])})
    async with FakeOpsPlatformConnector(customized) as reader:
        result = await reader.get_application("payment-service")
        assert result.name == "自定义样例"
        with pytest.raises(ValidationError, match="frozen"):
            result.name = "changed"  # type: ignore[misc]
        assert (await reader.get_application("payment-service")).name == "自定义样例"


@pytest.mark.parametrize("corrupt", ["duplicate", "business", "owner", "cycle", "ticket"])
def test_inconsistent_fake_snapshot_rejected(corrupt: str) -> None:
    snapshot = sample_snapshot()
    changes: dict[str, object]
    if corrupt == "duplicate":
        changes = {"owners": (*snapshot.owners, snapshot.owners[0])}
    elif corrupt in {"business", "owner"}:
        fields = (
            {"business_id": "missing"} if corrupt == "business" else {"owner_ids": ("missing",)}
        )
        changes = {"applications": (snapshot.applications[0].model_copy(update=fields),)}
    elif corrupt == "cycle":
        changes = {
            "service_tree": (snapshot.service_tree[0].model_copy(update={"parent_id": "weipai"}),)
        }
    else:
        changes = {"tickets": (snapshot.tickets[0].model_copy(update={"service_name": "missing"}),)}
    with pytest.raises(ValidationError):
        FakeOpsPlatformConnector(snapshot.model_copy(update=changes))


@pytest.mark.asyncio
async def test_demo_isolates_unrelated_real_host_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CONNECTOR_MODE", "real")
    monkeypatch.setenv("CONNECTOR_READER_TOKENS", '{"ops_platform":"unused-real-secret"}')
    monkeypatch.setenv("OPS_PLATFORM_CONFIG", json.dumps(CONFIG))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://sample:sample@database.invalid/weipai")
    monkeypatch.setenv("LLM_MODE", "gateway")
    await demo()
    output = capsys.readouterr().out
    assert '"mode": "fake"' in output and '"business": "支付业务（样例）"' in output
    assert "Step 11 Fake 样例验收通过" in output
    assert "unused-real-secret" not in output
