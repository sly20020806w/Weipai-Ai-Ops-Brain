"""Step 13：Fake/HTTP mock 共享服务与 UTC 时间窗契约；禁止实际网络。"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import httpx2 as httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.config import Settings
from app.connectors.models import ExecutorCredentials, ReaderCredentials
from app.connectors.observability import http as signing
from app.connectors.observability.arms import HTTPARMSConnector
from app.connectors.observability.base import (
    ARMSConnector,
    ObservabilityError,
    ObservabilityHTTPError,
    ObservabilityResponseError,
    ObservabilityTimeout,
    PrometheusConnector,
    SLSConnector,
)
from app.connectors.observability.config import ARMSConfig, PrometheusConfig, SLSConfig
from app.connectors.observability.factory import (
    create_arms_connector,
    create_prometheus_connector,
    create_sls_connector,
)
from app.connectors.observability.fake import (
    SAMPLE_END,
    SAMPLE_START,
    FakeARMSConnector,
    FakePrometheusConnector,
    FakeSLSConnector,
    sample_logs,
    sample_metrics,
    sample_traces,
)
from app.connectors.observability.models import MetricsQuery, Window, topology
from app.connectors.observability.prometheus import HTTPPrometheusConnector
from app.connectors.observability.sls import HTTPSLSConnector

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
KEY = json.dumps(
    {
        "access_key_id": "mock-id",
        "access_key_secret": "mock-secret",
        "security_token": "mock-session",
    }
)
PROM = PrometheusConfig(base_url="https://prom.example.invalid/prefix/", timeout_seconds=1.25)
SLS = SLSConfig(
    base_url="https://fake-project.example.invalid",
    project="fake-project",
    logstore="payment-logs",
    page_size=2,
    timeout_seconds=1.25,
)
ARMS = ARMSConfig(
    base_url="https://arms.example.invalid",
    region_id="cn-hangzhou",
    page_size=2,
    timeout_seconds=1.25,
)


def window(service: str = "payment-service") -> Window:
    return Window(service_name=service, start=SAMPLE_START, end=SAMPLE_END)


def metric_query(service: str = "payment-service") -> MetricsQuery:
    return MetricsQuery(**window(service).model_dump())


def source() -> httpx.MockTransport:
    """源故意包含时间窗外数据，验证客户端二次筛选；按服务选择与原生分页。"""

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.extensions["timeout"] == dict(connect=1.25, read=1.25, write=1.25, pool=1.25)
        query = request.url.params
        if request.url.host == "prom.example.invalid":
            assert request.url.path == "/prefix/api/v1/query_range"
            assert request.headers["authorization"] == "Bearer mock-prom-reader"
            assert query["start"] == SAMPLE_START.isoformat()
            assert query["end"] == SAMPLE_END.isoformat() and query["step"] == "60"
            service = query["query"].split('"')[1]
            assert query["query"] == f'http_5xx_ratio{{service="{service}"}}'
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": series.labels,
                                "values": [
                                    [p.timestamp.timestamp(), str(p.value)] for p in series.points
                                ],
                            }
                            for series in sample_metrics()
                            if series.service_name == service
                        ],
                    },
                },
            )
        if request.url.host == "fake-project.example.invalid":
            assert request.url.path == "/logstores/payment-logs"
            assert request.headers["authorization"].startswith("LOG mock-id:")
            assert request.headers["x-acs-security-token"] == "mock-session"
            assert request.headers["x-log-apiversion"] == "0.6.0"
            assert query["from"] == str(int(SAMPLE_START.timestamp()))
            assert query["to"] == str(int(SAMPLE_END.timestamp()))
            assert query["type"] == "log" and query["reverse"] == "false"
            service = query["query"].split('"')[1]
            assert query["query"] == f'service_name: "{service}"'
            logs = [log for log in sample_logs() if log.service_name == service]
            offset, size = int(query["offset"]), int(query["line"])
            data = [
                {
                    "service_name": log.service_name,
                    "__time__": str(int(log.timestamp.timestamp())),
                    "level": log.level,
                    "message": log.message,
                    "unrelated": "discarded",
                }
                for log in logs[offset : offset + size]
            ]
            return httpx.Response(
                200,
                json=data,
                headers={"x-log-progress": "Complete", "x-log-count": str(len(data))},
            )
        assert request.url.host == "arms.example.invalid" and request.url.path == "/"
        assert request.headers["authorization"].startswith("ACS3-HMAC-SHA256 Credential=mock-id,")
        assert request.headers["x-acs-security-token"] == "mock-session"
        assert request.headers["x-acs-version"] == "2019-08-08"
        assert query["RegionId"] == "cn-hangzhou"
        assert query["StartTime"] == str(int(SAMPLE_START.timestamp() * 1000))
        assert query["EndTime"] == str(int(SAMPLE_END.timestamp() * 1000))
        page, size = int(query["PageNumber"]), int(query["PageSize"])
        offset = (page - 1) * size
        if request.headers["x-acs-action"] == "SearchTracesByPage":
            traces = [t for t in sample_traces() if t.service_name == query["ServiceName"]]
            return httpx.Response(
                200,
                json={
                    "RequestId": "mock",
                    "PageBean": {
                        "PageNumber": page,
                        "PageSize": size,
                        "Total": len(traces),
                        "TraceInfos": [
                            {
                                "TraceID": t.trace_id,
                                "ServiceName": t.service_name,
                                "Timestamp": int(t.timestamp.timestamp() * 1000),
                                "Duration": t.duration_ms,
                            }
                            for t in traces[offset : offset + size]
                        ],
                    },
                },
            )
        assert request.headers["x-acs-action"] == "GetTrace"
        trace = next(t for t in sample_traces() if t.trace_id == query["TraceID"])
        return httpx.Response(
            200,
            json={
                "Spans": [
                    {
                        "TraceID": span.trace_id,
                        "SpanId": span.span_id,
                        "ParentSpanId": span.parent_span_id,
                        "ServiceName": span.service_name,
                        "Timestamp": int(span.timestamp.timestamp() * 1000),
                        "Duration": span.duration_ms,
                        "OperationName": span.operation,
                        "ResultCode": span.result_code,
                        "unrelated": "discarded",
                    }
                    for span in trace.spans[offset : offset + size]
                ]
            },
        )

    return httpx.MockTransport(handle)


def http_readers(
    transport: httpx.MockTransport,
) -> tuple[PrometheusConnector, SLSConnector, ARMSConnector]:
    return (
        HTTPPrometheusConnector(
            PROM,
            ReaderCredentials(connector="prometheus", token="mock-prom-reader"),
            transport=transport,
        ),
        HTTPSLSConnector(SLS, ReaderCredentials(connector="sls", token=KEY), transport=transport),
        HTTPARMSConnector(
            ARMS, ReaderCredentials(connector="arms", token=KEY), transport=transport
        ),
    )


@pytest_asyncio.fixture(params=["fake", "http-mock"])
async def readers(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[PrometheusConnector, SLSConnector, ARMSConnector]]:
    clients = (
        (FakePrometheusConnector(), FakeSLSConnector(), FakeARMSConnector())
        if request.param == "fake"
        else http_readers(source())
    )
    async with AsyncExitStack() as stack:
        for client in clients:
            await stack.enter_async_context(client)
        yield clients


@pytest.mark.parametrize("service", ["payment-service", "checkout-service", "missing"])
@pytest.mark.asyncio
async def test_shared_service_utc_boundary_and_topology_contract(
    readers: tuple[PrometheusConnector, SLSConnector, ARMSConnector],
    service: str,
) -> None:
    prom, sls, arms = readers
    query = window(service)
    series = await prom.query_metrics(metric_query(service))
    logs = await sls.query_logs(query)
    traces = await arms.query_traces(query)
    if service == "missing":
        assert series == () and logs == () and traces == ()
        return
    assert len(series) == 1 and len(series[0].points) == len(logs) == len(traces) == 2
    timestamps = [SAMPLE_START, SAMPLE_START + timedelta(minutes=5)]
    assert [p.timestamp for p in series[0].points] == timestamps
    assert [log.timestamp for log in logs] == [t.timestamp for t in traces] == timestamps
    assert series[0].service_name == service
    assert all(
        log.service_name == t.service_name == service for log, t in zip(logs, traces, strict=True)
    )
    assert all(t.tzinfo is UTC for t in timestamps)
    assert all(query.contains(span.timestamp) for trace in traces for span in trace.spans)
    edges = topology(traces)
    assert len(edges) == 2
    assert all(e.source_service == service and e.target_service == "payment-db" for e in edges)
    assert all(e.parent_span_id == "root" and e.span_id == "db" for e in edges)


@pytest.mark.asyncio
async def test_closed_clients_and_no_write_interface(
    readers: tuple[PrometheusConnector, SLSConnector, ARMSConnector],
) -> None:
    for client in readers:
        assert not any(hasattr(client, name) for name in ("write", "delete", "execute", "request"))
        await client.aclose()
        await client.aclose()
    for call in (
        readers[0].query_metrics(metric_query()),
        readers[1].query_logs(window()),
        readers[2].query_traces(window()),
    ):
        with pytest.raises(ObservabilityError, match="已关闭"):
            await call


@pytest.mark.parametrize(
    "values",
    [
        {"service_name": ""},
        {"service_name": 'x" or *'},
        {"service_name": "../payment"},
        {"start": datetime(2026, 10, 1)},
        {"end": SAMPLE_START},
        {"end": SAMPLE_START - timedelta(seconds=1)},
        {"end": SAMPLE_START + timedelta(days=2)},
        {"token": "forbidden"},
        {"base_url": "https://other.invalid"},
    ],
)
def test_invalid_time_window_or_identity_is_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Window.model_validate({**window().model_dump(), **values})


def test_timezone_normalization_and_metric_bounds() -> None:
    local = SAMPLE_START.astimezone(timezone(timedelta(hours=8)))
    query = Window(service_name="payment-service", start=local, end=local + timedelta(minutes=10))
    assert query.start == SAMPLE_START and query.start.tzinfo is UTC
    for values in (
        {"metric_name": "up{}"},
        {"metric_name": "x or up"},
        {"step_seconds": 0},
        {"step_seconds": True},
        {"end": SAMPLE_START + timedelta(days=1), "step_seconds": 1},
    ):
        with pytest.raises(ValidationError):
            MetricsQuery.model_validate({**query.model_dump(), **values})


@pytest.mark.asyncio
async def test_fake_snapshot_isolation_and_span_cutoff() -> None:
    data = sample_metrics()
    async with FakePrometheusConnector(data) as prom:
        data[0].labels.clear()
        result = await prom.query_metrics(metric_query())
        result[0].labels.clear()
        assert (await prom.query_metrics(metric_query()))[0].labels["service"] == "payment-service"
        assert (
            await prom.query_metrics(MetricsQuery(**window().model_dump(), metric_name="missing"))
            == ()
        )
    async with FakeARMSConnector() as arms:
        traces = await arms.query_traces(
            Window(
                service_name="payment-service",
                start=SAMPLE_START,
                end=SAMPLE_START + timedelta(milliseconds=500),
            )
        )
        assert len(traces) == 1 and len(traces[0].spans) == 1 and topology(traces) == ()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://host",
        "https://u:p@host",
        "https://host?token=x",
        "https://host#fragment",
        "https://host/../other",
        "https://host/%2e%2e",
        "https://host\\x",
        "https://host:0",
        "https://host:bad",
        "https://host/ white",
    ],
)
def test_invalid_endpoint(base_url: str) -> None:
    with pytest.raises(ValidationError):
        PrometheusConfig(base_url=base_url)


@pytest.mark.parametrize("kind", ["prometheus", "sls", "arms"])
@pytest.mark.asyncio
async def test_factory_configuration_and_reader_separation(kind: str) -> None:
    factories = {
        "prometheus": create_prometheus_connector,
        "sls": create_sls_connector,
        "arms": create_arms_connector,
    }
    expected = {
        "prometheus": FakePrometheusConnector,
        "sls": FakeSLSConnector,
        "arms": FakeARMSConnector,
    }
    async with factories[kind](Settings(APP_ENV="test")) as fake:
        assert isinstance(fake, expected[kind]) and fake.reader_credentials is None
    config = Settings.model_validate(
        {
            "APP_ENV": "staging",
            "CONNECTOR_MODE": "real",
            "PROMETHEUS_CONFIG": PROM,
            "SLS_CONFIG": SLS,
            "ARMS_CONFIG": ARMS,
            "CONNECTOR_READER_TOKENS": {"prometheus": "mock-prom-reader", "sls": KEY, "arms": KEY},
        }
    )
    async with factories[kind](config, transport=source()) as real:
        assert real.reader_credentials is not None and real.reader_credentials.connector == kind
    for env in ("local", "test"):
        with pytest.raises(ValidationError):
            Settings(APP_ENV=env, CONNECTOR_MODE="real")
    with pytest.raises(ValueError, match="只读凭证"):
        factories[kind](Settings(APP_ENV="staging", CONNECTOR_MODE="real"))
    config.connector_reader_tokens.clear()
    with pytest.raises(ValueError, match="只读凭证"):
        factories[kind](config)
    constructor: Callable[..., object] = {
        "prometheus": HTTPPrometheusConnector,
        "sls": HTTPSLSConnector,
        "arms": HTTPARMSConnector,
    }[kind]
    provider_config = {"prometheus": PROM, "sls": SLS, "arms": ARMS}[kind]
    with pytest.raises(TypeError, match="ReaderCredentials"):
        constructor(provider_config, ExecutorCredentials(connector=kind, token="writer"))
    with pytest.raises(ValueError, match="Reader 凭证"):
        constructor(provider_config, ReaderCredentials(connector="other", token=KEY))


@pytest.mark.parametrize("kind", ["prometheus", "sls", "arms"])
@pytest.mark.parametrize("failure", ["timeout", "connection", "redirect", "forbidden"])
@pytest.mark.asyncio
async def test_http_failure_is_sanitized(kind: str, failure: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("mock-secret URL", request=request)
        if failure == "connection":
            raise httpx.ConnectError("mock-secret URL", request=request)
        return httpx.Response(
            302 if failure == "redirect" else 403,
            text="mock-secret",
            headers={"Location": "https://production.invalid"},
        )

    clients = http_readers(httpx.MockTransport(handle))
    async with AsyncExitStack() as stack:
        for client in clients:
            await stack.enter_async_context(client)
        with pytest.raises(ObservabilityError) as error:
            if kind == "prometheus":
                await clients[0].query_metrics(metric_query())
            elif kind == "sls":
                await clients[1].query_logs(window())
            else:
                await clients[2].query_traces(window())
        assert "mock-secret" not in str(error.value) and "URL" not in str(error.value)
        if failure == "timeout":
            assert isinstance(error.value, ObservabilityTimeout)
        elif failure in {"redirect", "forbidden"}:
            assert isinstance(error.value, ObservabilityHTTPError)


@pytest.mark.parametrize(
    "fault",
    [
        "nan",
        "wrong_service",
        "wrong_metric",
        "warnings",
        "type",
        "duplicate_time",
        "bad_json",
        "too_many",
    ],
)
@pytest.mark.asyncio
async def test_prometheus_invalid_protocol(fault: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if fault == "bad_json":
            return httpx.Response(200, text="not json mock-secret")
        data = cast(dict[str, Any], source().handle_request(request).json())
        series = data["data"]["result"][0]
        if fault == "nan":
            series["values"][0][1] = "NaN"
        elif fault == "wrong_service":
            series["metric"]["service"] = "other"
        elif fault == "wrong_metric":
            series["metric"]["__name__"] = "up"
        elif fault == "warnings":
            data["warnings"] = ["partial response mock-secret"]
        elif fault == "type":
            data["data"]["resultType"] = "vector"
        elif fault == "duplicate_time":
            series["values"].append(series["values"][0])
        else:
            data["data"]["result"] *= 1001
        return httpx.Response(200, json=data)

    async with HTTPPrometheusConnector(
        PROM,
        ReaderCredentials(connector="prometheus", token="mock-prom-reader"),
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ObservabilityResponseError) as error:
            await client.query_metrics(metric_query())
        assert "mock-secret" not in str(error.value)


@pytest.mark.parametrize(
    "fault",
    [
        "incomplete",
        "wrong_count",
        "wrong_service",
        "missing_time",
        "bad_time",
        "bad_json",
        "max_pages",
    ],
)
@pytest.mark.asyncio
async def test_sls_invalid_protocol(fault: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        original = source().handle_request(request)
        data = cast(list[dict[str, str]], original.json())
        headers = dict(original.headers)
        if fault == "bad_json":
            return httpx.Response(200, text="mock-secret", headers=headers)
        if fault == "incomplete":
            headers["x-log-progress"] = "Incomplete"
        elif fault == "wrong_count":
            headers["x-log-count"] = "invalid"
        elif fault == "wrong_service":
            data[0]["service_name"] = "other"
        elif fault == "missing_time":
            del data[0]["__time__"]
        elif fault == "bad_time":
            data[0]["__time__"] = "NaN"
        return httpx.Response(200, json=data, headers=headers)

    config = SLS.model_copy(update={"max_pages": 1}) if fault == "max_pages" else SLS
    async with HTTPSLSConnector(
        config, ReaderCredentials(connector="sls", token=KEY), transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(ObservabilityResponseError):
            await client.query_logs(window())


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_page",
        "duplicate_trace",
        "changed_total",
        "empty_page",
        "wrong_service",
        "wrong_span_trace",
        "duplicate_span",
        "bad_unit",
        "business_error",
        "max_pages",
    ],
)
@pytest.mark.asyncio
async def test_arms_invalid_protocol(fault: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        data = cast(dict[str, Any], source().handle_request(request).json())
        if fault == "business_error":
            return httpx.Response(200, json={"Code": "mock-secret", "Message": "mock-secret"})
        if request.headers["x-acs-action"] == "SearchTracesByPage":
            page = data["PageBean"]
            if fault == "wrong_page":
                page["PageNumber"] += 1
            elif fault == "duplicate_trace":
                page["TraceInfos"][1] = page["TraceInfos"][0]
            elif fault == "changed_total" and int(request.url.params["PageNumber"]) == 2:
                page["Total"] += 1
            elif fault == "empty_page":
                page["TraceInfos"] = []
            elif fault == "wrong_service":
                page["TraceInfos"][0]["ServiceName"] = "other"
        else:
            if fault == "wrong_span_trace" and data["Spans"]:
                data["Spans"][0]["TraceID"] = "other"
            elif fault == "duplicate_span" and int(request.url.params["PageNumber"]) == 2:
                return source().handle_request(
                    httpx.Request(
                        "GET",
                        str(request.url).replace("PageNumber=2", "PageNumber=1"),
                        headers=request.headers,
                        extensions=request.extensions,
                    )
                )
            elif fault == "bad_unit" and data["Spans"]:
                data["Spans"][0]["Timestamp"] = "not-a-timestamp"
        return httpx.Response(200, json=data)

    config = ARMS.model_copy(update={"max_pages": 1}) if fault == "max_pages" else ARMS
    async with HTTPARMSConnector(
        config,
        ReaderCredentials(connector="arms", token=KEY),
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ObservabilityResponseError) as error:
            await client.query_traces(window())
        assert "mock-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_arms_nested_children_microseconds_and_clipped_spans() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        response = source().handle_request(request)
        data = cast(dict[str, Any], response.json())
        if request.headers["x-acs-action"] == "GetTrace":
            root, child = data["Spans"]
            root["Timestamp"] *= 1000
            child["Timestamp"] *= 1000
            outside = {
                **child,
                "SpanId": "outside",
                "Timestamp": int(SAMPLE_END.timestamp() * 1000000),
            }
            root["Children"] = [[child, outside]]
            data["Spans"] = {"Span": [root]}
        return httpx.Response(200, json=data)

    config = ARMS.model_copy(update={"span_timestamp_unit": "microseconds", "page_size": 100})
    async with HTTPARMSConnector(
        config,
        ReaderCredentials(connector="arms", token=KEY),
        transport=httpx.MockTransport(handle),
    ) as client:
        traces = await client.query_traces(window())
        assert len(traces) == 2 and all(len(trace.spans) == 2 for trace in traces)
        assert len(topology(traces)) == 2
        assert traces[0].spans[0].timestamp == SAMPLE_START
        assert traces[0].spans[1].timestamp == SAMPLE_START + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_subsecond_window_is_covered_then_clipped() -> None:
    start = SAMPLE_START + timedelta(microseconds=125001)
    end = SAMPLE_START + timedelta(minutes=5, microseconds=125001)
    query = Window(service_name="payment-service", start=start, end=end)

    def handle(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.host == "prom.example.invalid":
            assert params["start"] == start.isoformat() and params["end"] == end.isoformat()
            params.update(start=SAMPLE_START.isoformat(), end=SAMPLE_END.isoformat())
        elif request.url.host == "fake-project.example.invalid":
            assert params["from"] == str(int(SAMPLE_START.timestamp()))
            assert params["to"] == str(
                int((SAMPLE_START + timedelta(minutes=5, seconds=1)).timestamp())
            )
            params.update(
                {"from": str(int(SAMPLE_START.timestamp())), "to": str(int(SAMPLE_END.timestamp()))}
            )
        else:
            assert params["StartTime"] == str(int(SAMPLE_START.timestamp() * 1000) + 125)
            assert params["EndTime"] == str(int(SAMPLE_START.timestamp() * 1000) + 300126)
            params.update(
                StartTime=str(int(SAMPLE_START.timestamp() * 1000)),
                EndTime=str(int(SAMPLE_END.timestamp() * 1000)),
            )
        return source().handle_request(
            httpx.Request(
                "GET",
                str(request.url.copy_with(query=None)),
                params=params,
                headers=request.headers,
                extensions=request.extensions,
            )
        )

    clients = http_readers(httpx.MockTransport(handle))
    async with AsyncExitStack() as stack:
        for client in clients:
            await stack.enter_async_context(client)
        metrics = await clients[0].query_metrics(MetricsQuery(**query.model_dump()))
        logs = await clients[1].query_logs(query)
        traces = await clients[2].query_traces(query)
        expected = SAMPLE_START + timedelta(minutes=5)
        assert [p.timestamp for p in metrics[0].points] == [expected]
        assert [log.timestamp for log in logs] == [expected]
        assert [t.timestamp for t in traces] == [expected]
        assert len(traces[0].spans) == 1 and topology(traces) == ()


@pytest.mark.parametrize("kind", ["sls", "arms"])
@pytest.mark.parametrize(
    "token",
    [
        "not json",
        "{}",
        '{"access_key_id":"mock-secret"}',
        '{"access_key_id":"id","access_key_secret":""}',
        '{"access_key_id":"id","access_key_secret":"secret","security_token":"line\\nvalue"}',
    ],
)
def test_alibaba_reader_errors_hide_credentials(kind: str, token: str) -> None:
    credentials = ReaderCredentials(connector=kind, token=token)
    with pytest.raises(ValueError) as error:
        if kind == "sls":
            HTTPSLSConnector(SLS, credentials)
        else:
            HTTPARMSConnector(ARMS, credentials)
    assert "mock-secret" not in str(error.value)


@pytest.mark.parametrize("kind", ["prometheus", "sls", "arms"])
def test_missing_provider_config_and_modified_local_mode(kind: str) -> None:
    factories = {
        "prometheus": create_prometheus_connector,
        "sls": create_sls_connector,
        "arms": create_arms_connector,
    }
    settings = Settings.model_validate(
        {
            "APP_ENV": "staging",
            "CONNECTOR_MODE": "real",
            "CONNECTOR_READER_TOKENS": {kind: "mock-prom-reader" if kind == "prometheus" else KEY},
        }
    )
    with pytest.raises(ValueError, match="CONFIG"):
        factories[kind](settings, transport=source())
    unsafe = Settings(APP_ENV="local").model_copy(update={"connector_mode": "real"})
    with pytest.raises(ValidationError, match="只允许 fake"):
        factories[kind](unsafe, transport=source())


@pytest.mark.asyncio
async def test_fake_demo_isolated_from_host_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.connectors.observability.__main__ import main

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CONNECTOR_MODE", "real")
    monkeypatch.setenv("PROMETHEUS_CONFIG", "malformed")
    monkeypatch.setenv("SLS_CONFIG", "malformed")
    monkeypatch.setenv("ARMS_CONFIG", "malformed")
    await main()
    output = capsys.readouterr().out
    assert "Step 13 Fake 样例验收通过" in output and '"mode": "fake"' in output
    assert '"target_service": "payment-db"' in output


def test_native_signing_matches_fixed_protocol_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """参考值由固定 canonical message 独立计算，捕获排序/编码/空体/TLS host 签名偏差。"""
    from types import SimpleNamespace

    class Clock:
        @staticmethod
        def now(zone: object = None) -> datetime:
            return SAMPLE_START

    monkeypatch.setattr(signing, "datetime", Clock)
    monkeypatch.setattr(signing, "uuid4", lambda: SimpleNamespace(hex="fixed-nonce"))
    key = signing.reader_key(ReaderCredentials(connector="sls", token=KEY))
    params = {
        "type": "log",
        "query": 'service_name: "payment-service"',
        "to": "1790817000",
        "from": "1790816400",
    }
    headers = signing.sls_headers(key, "/logstores/payment-logs", params)
    assert headers["Authorization"] == "LOG mock-id:mdtnNQRUp/q3WplDrhZxTTfSKCk="
    params = {"ServiceName": "payment-service", "RegionId": "cn-hangzhou"}
    headers = signing.arms_headers(key, "arms.example.invalid", "SearchTracesByPage", params)
    assert headers["Authorization"].endswith(
        "Signature=a85f9c25a2d3a1345568f56802aea571b7b22beab65206005e2a75f84d7333f9"
    )
    assert "x-acs-security-token" in headers["Authorization"]
    tampered = signing.arms_headers(
        key,
        "arms.example.invalid",
        "SearchTracesByPage",
        {**params, "ServiceName": "other-service"},
    )
    assert tampered["Authorization"] != headers["Authorization"]
