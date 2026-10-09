"""Step 15：真实接口只经 MockTransport，所有测试阻止实际联网。"""

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx2 as httpx
import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from app.config import Settings
from app.connectors.cloud.base import (
    CloudError,
    CloudHTTPError,
    CloudNotFound,
    CloudResponseError,
    CloudTimeout,
)
from app.connectors.cloud.client import HTTPCloudConnector
from app.connectors.cloud.config import CloudConfig, ResourceBinding
from app.connectors.cloud.factory import create_cloud_connector
from app.connectors.cloud.fake import (
    SAMPLE_END,
    SAMPLE_IDS,
    SAMPLE_START,
    SAMPLE_TIME,
    FakeCloudConnector,
    sample_events,
    sample_resources,
)
from app.connectors.cloud.http import READ_APIS, CloudHTTP, ReadAPI, signed_headers
from app.connectors.cloud.models import CloudQuery, Product, RDSConnections
from app.connectors.models import ExecutorCredentials, ReaderCredentials
from app.connectors.observability.config import AlibabaReaderKey
from app.tools.models import JsonObject

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
KEY_JSON = json.dumps(
    {
        "access_key_id": "mock-id",
        "access_key_secret": "mock-secret",
        "security_token": "mock-sts",
    }
)
QUERY = CloudQuery(service_name="payment-service", start=SAMPLE_START, end=SAMPLE_END)
EVENT: JsonObject = {
    "Id": "event-rds",
    "Time": int(SAMPLE_TIME.timestamp() * 1000),
    "ResourceId": "rm-payment",
    "RegionId": "cn-hangzhou",
    "Name": "RDSConnectionUsageHigh",
    "Level": "WARN",
    "Status": "alert",
    "Content": "must-not-copy-secret",
}
PERFORMANCE: JsonObject = {
    "DBInstanceId": "rm-payment",
    "Engine": "MySQL",
    "PerformanceKeys": {
        "PerformanceKey": [
            {
                "Key": "MySQL_Sessions",
                "ValueFormat": "active_session&total_session",
                "Values": {
                    "PerformanceValue": [{"Date": "2026-10-01T01:08:00Z", "Value": "480&520"}]
                },
            }
        ]
    },
}
RESPONSES: dict[str, JsonObject] = {
    "DescribeInstances": {
        "TotalCount": 1,
        "PageNumber": 1,
        "PageSize": 1,
        "Instances": {
            "Instance": [
                {
                    "InstanceId": "i-payment",
                    "RegionId": "cn-hangzhou",
                    "Status": "Running",
                    "InstanceType": "ecs.g7.large",
                }
            ]
        },
    },
    "DescribeDBInstanceAttribute": {
        "Items": {
            "DBInstanceAttribute": [
                {
                    "DBInstanceId": "rm-payment",
                    "DBInstanceStatus": "Running",
                    "RegionId": "cn-hangzhou",
                    "Engine": "MySQL",
                    "EngineVersion": "8.0",
                    "MaxConnections": 600,
                    "DisasterRecoveryInfo": "must-not-copy-secret",
                }
            ]
        }
    },
    "DescribeInstanceAttribute": {
        "Instances": {
            "DBInstanceAttribute": [
                {
                    "InstanceId": "r-payment",
                    "InstanceStatus": "Normal",
                    "RegionId": "cn-hangzhou",
                    "EngineVersion": "7.0",
                    "Config": "must-not-copy-secret",
                }
            ]
        }
    },
    "OnsInstanceBaseInfo": {
        "InstanceBaseInfo": {"InstanceId": "MQ_INST_payment", "InstanceStatus": 5}
    },
    "DescribeLoadBalancerAttribute": {
        "LoadBalancerId": "lb-payment",
        "LoadBalancerStatus": "active",
    },
    "DescribeVpcAttribute": {
        "VpcId": "vpc-payment",
        "Status": "Available",
        "RegionId": "cn-hangzhou",
    },
    "DescribeDomainInfo": {
        "DomainName": "payment.example.com",
        "InBlackHole": False,
        "InClean": False,
    },
    "DescribeCdnDomainDetail": {
        "GetDomainDetailModel": {
            "DomainName": "static.payment.example.com",
            "DomainStatus": "online",
            "Cname": "static.example.com.w.kunlun.com",
        }
    },
    "DescribeDBInstancePerformance": PERFORMANCE,
    "DescribeSystemEventAttribute": {
        "Code": "200",
        "Success": "true",
        "SystemEvents": {"SystemEvent": [EVENT]},
    },
}


def config(products: tuple[Product, ...] = tuple(SAMPLE_IDS), **updates: object) -> CloudConfig:
    return CloudConfig.model_validate(
        {
            "endpoints": {api.product: f"https://{api.product}.invalid" for api in READ_APIS},
            "services": {
                "payment-service": [
                    {
                        "product": p,
                        "resource_id": SAMPLE_IDS[p],
                        "region_id": "cn-hangzhou",
                    }
                    for p in products
                ]
            },
            **updates,
        }
    )


def credentials() -> ReaderCredentials:
    return ReaderCredentials(connector="cloud", token=SecretStr(KEY_JSON))


def mock_handler(request: httpx.Request) -> httpx.Response:
    action = request.headers["x-acs-action"]
    api = next(api for api in READ_APIS if api.action == action)
    assert request.method == "GET" and request.url.path == "/"
    assert request.url.host == api.product + ".invalid"
    assert request.headers["x-acs-version"] == api.version
    assert request.headers["x-acs-security-token"] == "mock-sts"
    assert request.headers["Authorization"].startswith("ACS3-HMAC-SHA256 Credential=mock-id,")
    assert "x-acs-security-token" in request.headers["Authorization"]
    assert "mock-secret" not in str(request.url) and "mock-sts" not in str(request.url)
    params = request.url.params
    if action == "DescribeInstances":
        assert json.loads(params["InstanceIds"]) == ["i-payment"]
        assert params["RegionId"] == "cn-hangzhou"
    elif action == "DescribeDBInstancePerformance":
        assert params["DBInstanceId"] == "rm-payment" and params["Key"] == "MySQL_Sessions"
        assert params["StartTime"] == "2026-10-01T01:00Z"
        assert params["EndTime"] == "2026-10-01T01:10Z"
    elif action == "DescribeSystemEventAttribute":
        assert params["StartTime"] == str(int(SAMPLE_START.timestamp() * 1000))
        assert params["EndTime"] == str(int(SAMPLE_END.timestamp() * 1000))
    return httpx.Response(200, json=RESPONSES[action])


@pytest.mark.asyncio
async def test_all_native_resources_connections_and_events() -> None:
    actions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        actions.append(request.headers["x-acs-action"])
        return mock_handler(request)

    async with HTTPCloudConnector(
        config(), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        result = await reader.get_cloud_resources(QUERY)
        assert {r.product for r in result.resources} == set(SAMPLE_IDS)
        assert len(result.resources) == 8 and len(result.events) == 1
        rds = next(r for r in result.resources if r.product == "rds")
        assert rds.rds_connections == RDSConnections(
            availability="available",
            max_connections=600,
            sampled_at=SAMPLE_TIME,
            active_connections=480.0,
            total_connections=520.0,
        )
        assert next(r for r in result.resources if r.product == "dns").status is None
        assert result.collected_at.tzinfo is UTC
        assert result.events[0].product == "rds" and result.events[0].resource_id == "rm-payment"
        assert "must-not-copy-secret" not in result.model_dump_json()
        assert "mock-secret" not in result.model_dump_json() and "mock-sts" not in repr(
            reader.reader_credentials
        )
        assert set(actions) == {api.action for api in READ_APIS if api.action != "OnsTopicList"}
    with pytest.raises(CloudError, match="已关闭"):
        await reader.get_cloud_resources(QUERY)


@pytest.mark.parametrize("product", SAMPLE_IDS)
@pytest.mark.asyncio
async def test_single_product_only_reads_bound_id(product: Product) -> None:
    async with HTTPCloudConnector(
        config((product,)), credentials(), transport=httpx.MockTransport(mock_handler)
    ) as reader:
        result = await reader.get_cloud_resources(QUERY)
        assert [r.resource_id for r in result.resources] == [SAMPLE_IDS[product]]
        assert len(result.events) == (1 if product == "rds" else 0)


@pytest.mark.asyncio
async def test_fake_acceptance_scope_window_and_snapshot_isolation() -> None:
    other = sample_resources()[0].model_copy(
        update={"service_name": "other-service", "resource_id": "i-other"}
    )
    async with FakeCloudConnector(resources=(*sample_resources(), other)) as reader:
        result = await reader.get_cloud_resources(CloudQuery(service_name="payment-service"))
        assert len(result.resources) == 8 and len(result.events) == 1
        assert result.end == SAMPLE_END and result.end - result.start == timedelta(minutes=15)
        assert all(r.service_name == "payment-service" for r in result.resources)
        object.__setattr__(result.resources[0].details, "cname", "tampered")
        again = await reader.get_cloud_resources(QUERY)
        assert again.resources[0].details.cname != "tampered"
        after = await reader.get_cloud_resources(
            CloudQuery(
                service_name="payment-service",
                start=SAMPLE_TIME + timedelta(microseconds=1),
                end=SAMPLE_END,
            )
        )
        assert after.events == ()
        rds = next(r for r in after.resources if r.product == "rds")
        assert rds.rds_connections is not None and rds.rds_connections.availability == "no_data"
        assert rds.rds_connections.total_connections is None
        at_end = await reader.get_cloud_resources(
            CloudQuery(
                service_name="payment-service",
                start=SAMPLE_START,
                end=SAMPLE_TIME,
            )
        )
        assert at_end.events == ()
        at_start = await reader.get_cloud_resources(
            CloudQuery(
                service_name="payment-service",
                start=SAMPLE_TIME,
                end=SAMPLE_END,
            )
        )
        assert len(at_start.events) == 1
        with pytest.raises(CloudNotFound):
            await reader.get_cloud_resources(CloudQuery(service_name="unknown"))
    with pytest.raises(CloudError, match="已关闭"):
        await reader.get_cloud_resources(QUERY)


@pytest.mark.asyncio
async def test_factory_default_and_real_mock_and_environment_gate() -> None:
    fake = create_cloud_connector(Settings(APP_ENV="test"))
    assert isinstance(fake, FakeCloudConnector) and fake.reader_credentials is None
    assert not any(
        hasattr(fake, name) for name in ("delete", "execute_action", "restart", "write", "request")
    )
    await fake.aclose()
    settings = Settings(
        APP_ENV="production",
        CONNECTOR_MODE="real",
        CONNECTOR_READER_TOKENS={"cloud": KEY_JSON},
        CLOUD_CONFIG=config(),
    )
    async with create_cloud_connector(
        settings, transport=httpx.MockTransport(mock_handler)
    ) as real:
        assert isinstance(real, HTTPCloudConnector) and real.reader_credentials == credentials()
        assert len((await real.get_cloud_resources(QUERY)).resources) == 8
    for env in ("local", "test"):
        with pytest.raises(ValidationError, match="只允许 fake"):
            Settings(APP_ENV=env, CONNECTOR_MODE="real")
        modified = settings.model_copy(update={"app_env": env})
        with pytest.raises(ValidationError, match="只允许 fake"):
            create_cloud_connector(modified)
    with pytest.raises(ValueError, match="CLOUD_CONFIG"):
        create_cloud_connector(
            Settings(
                APP_ENV="production",
                CONNECTOR_MODE="real",
                CONNECTOR_READER_TOKENS={"cloud": KEY_JSON},
            )
        )
    with pytest.raises(ValueError, match="只读凭证"):
        create_cloud_connector(
            Settings(APP_ENV="production", CONNECTOR_MODE="real", CLOUD_CONFIG=config())
        )


def test_credentials_types_validation_and_secret_redaction() -> None:
    with pytest.raises(TypeError, match="ReaderCredentials"):
        HTTPCloudConnector(
            config(),
            cast(
                ReaderCredentials, ExecutorCredentials(connector="cloud", token=SecretStr(KEY_JSON))
            ),
        )
    with pytest.raises(ValueError, match="cloud Reader"):
        HTTPCloudConnector(config(), ReaderCredentials(connector="rds", token=SecretStr(KEY_JSON)))
    for token in (
        "raw-secret",
        '{"access_key_id":"mock-id","access_key_secret":"raw-secret", "write":true}',
        '{"access_key_id":"bad id","access_key_secret":"raw-secret"}',
    ):
        with pytest.raises(ValueError) as error:
            HTTPCloudConnector(
                config(), ReaderCredentials(connector="cloud", token=SecretStr(token))
            )
        assert "raw-secret" not in str(error.value)
    with pytest.raises(TypeError, match="MockTransport"):
        HTTPCloudConnector(config(), credentials(), transport=cast(httpx.MockTransport, object()))


@pytest.mark.parametrize(
    "url",
    [
        "http://cloud.invalid",
        "https://u:p@cloud.invalid",
        "https://cloud.invalid/api",
        "https://cloud.invalid?token=secret",
        "https://cloud.invalid/#x",
        "https://cloud.invalid:0",
        "https://cloud.invalid\\evil",
        "https://cloud.invalid/%2e",
        "https://cloud.invalid\n",
    ],
)
def test_bad_endpoint_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        config(endpoints={"cms": url})


def test_config_missing_endpoint_duplicate_bindings_and_mutation() -> None:
    original = config()
    data = original.model_dump()
    data["endpoints"].pop("rds")
    with pytest.raises(ValidationError, match="产品端点"):
        CloudConfig.model_validate(data)
    data = original.model_dump()
    data["endpoints"].pop("cms")
    with pytest.raises(ValidationError, match="cms"):
        CloudConfig.model_validate(data)
    data = original.model_dump()
    data["services"]["payment-service"] += (data["services"]["payment-service"][0],)
    with pytest.raises(ValidationError, match="不能重复"):
        CloudConfig.model_validate(data)
    tampered = original.model_copy(update={"endpoints": {"cms": "http://evil.invalid"}})
    with pytest.raises(ValidationError):
        HTTPCloudConnector(tampered, credentials())


@pytest.mark.parametrize(
    "product,identifier",
    [
        ("ecs", "../id"),
        ("ecs", "i/evil"),
        ("dns", "example..com"),
        ("dns", "UPPER.example.com"),
        ("cdn", "-bad.example.com"),
        ("cdn", "localhost"),
    ],
)
def test_bad_resource_bindings(product: str, identifier: str) -> None:
    with pytest.raises(ValidationError):
        ResourceBinding(product=product, resource_id=identifier, region_id="cn-hangzhou")


@pytest.mark.parametrize(
    "values",
    [
        {"start": SAMPLE_START},
        {"end": SAMPLE_END},
        {"start": SAMPLE_END, "end": SAMPLE_START},
        {"start": SAMPLE_START, "end": SAMPLE_START},
        {"start": SAMPLE_START, "end": SAMPLE_START + timedelta(days=2)},
        {"start": datetime(2026, 10, 1), "end": SAMPLE_END},
    ],
)
def test_invalid_query_time_windows(values: dict[str, datetime]) -> None:
    with pytest.raises(ValidationError):
        CloudQuery.model_validate({"service_name": "payment-service", **values})


@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
@pytest.mark.asyncio
async def test_http_errors_no_redirect_or_secret_leak(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, headers={"Location": "https://evil.invalid"}, text="mock-secret"
        )

    async with HTTPCloudConnector(
        config(("vpc",)), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        with pytest.raises(CloudHTTPError) as error:
            await reader.get_cloud_resources(QUERY)
        assert error.value.status_code == status and "mock-secret" not in str(error.value)
        assert calls == 1


@pytest.mark.parametrize("timeout", [True, False])
@pytest.mark.asyncio
async def test_transport_errors_are_sanitized(timeout: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if timeout:
            raise httpx.ReadTimeout("mock-secret", request=request)
        raise httpx.ConnectError("mock-secret", request=request)

    async with HTTPCloudConnector(
        config(("vpc",)), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        with pytest.raises(CloudTimeout if timeout else CloudError) as error:
            await reader.get_cloud_resources(QUERY)
        assert "mock-secret" not in str(error.value)


@pytest.mark.parametrize(
    "data",
    [
        {"Code": "Forbidden", "Message": "mock-secret"},
        {},
        {"VpcId": "vpc-other", "Status": "Available"},
        {"VpcId": "vpc-payment", "Status": "Available", "RegionId": "cn-beijing"},
    ],
)
@pytest.mark.asyncio
async def test_business_error_and_wrong_resource_fail(data: JsonObject) -> None:
    async with HTTPCloudConnector(
        config(("vpc",)),
        credentials(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)),
    ) as reader:
        with pytest.raises(CloudResponseError) as error:
            await reader.get_cloud_resources(QUERY)
        assert "mock-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_json_unknown_service_and_empty_bindings() -> None:
    async with HTTPCloudConnector(
        config(("vpc",)),
        credentials(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="bad secret")),
    ) as reader:
        with pytest.raises(CloudResponseError, match="JSON"):
            await reader.get_cloud_resources(QUERY)
        with pytest.raises(CloudNotFound):
            await reader.get_cloud_resources(CloudQuery(service_name="unknown"))

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("空资源绑定不能访问云 API")

    async with HTTPCloudConnector(
        config(()), credentials(), transport=httpx.MockTransport(unexpected)
    ) as reader:
        assert (await reader.get_cloud_resources(QUERY)).resources == ()
        assert (await reader.get_cloud_resources(QUERY)).events == ()


@pytest.mark.asyncio
async def test_cloud_events_complete_pages_exact_binding_and_half_open_window() -> None:
    events = [
        {**EVENT, "Id": "at-start", "Time": int(SAMPLE_START.timestamp() * 1000)},
        {**EVENT, "Id": "arn", "ResourceId": "acs:rds:cn-hangzhou:123:dbinstance/rm-payment"},
        {**EVENT, "Id": "other", "ResourceId": "rm-payment-other"},
        {**EVENT, "Id": "wrong-region", "RegionId": "cn-beijing"},
        {**EVENT, "Id": "before", "Time": int(SAMPLE_START.timestamp() * 1000) - 1},
        {**EVENT, "Id": "at-end", "Time": int(SAMPLE_END.timestamp() * 1000)},
    ]
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["x-acs-action"] != "DescribeSystemEventAttribute":
            return mock_handler(request)
        page = int(request.url.params["PageNumber"])
        pages.append(page)
        return httpx.Response(
            200,
            json={
                "Code": "200",
                "Success": True,
                "SystemEvents": {
                    "SystemEvent": events[(page - 1) * 2 : page * 2],
                },
            },
        )

    async with HTTPCloudConnector(
        config(("rds",), page_size=2), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        result = await reader.get_cloud_resources(QUERY)
        assert [e.id for e in result.events] == ["at-start"] and pages == [1, 2, 3, 4]
    bound = config(("rds",), page_size=2).model_dump()
    bound["services"]["payment-service"][0]["event_resource_id"] = (
        "acs:rds:cn-hangzhou:123:dbinstance/rm-payment"
    )
    async with HTTPCloudConnector(
        CloudConfig.model_validate(bound), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        assert [e.id for e in (await reader.get_cloud_resources(QUERY)).events] == ["arn"]


@pytest.mark.parametrize("repeat", [True, False])
@pytest.mark.asyncio
async def test_event_duplicate_and_pagination_limit_fail(repeat: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["x-acs-action"] != "DescribeSystemEventAttribute":
            return mock_handler(request)
        page = request.url.params["PageNumber"]
        return httpx.Response(
            200,
            json={
                "Code": "200",
                "Success": "true",
                "SystemEvents": {
                    "SystemEvent": [{**EVENT, "Id": "repeat" if repeat else "event-" + page}],
                },
            },
        )

    async with HTTPCloudConnector(
        config(("rds",), page_size=1, max_pages=2),
        credentials(),
        transport=httpx.MockTransport(handler),
    ) as reader:
        with pytest.raises(CloudResponseError, match="重复|分页上限"):
            await reader.get_cloud_resources(QUERY)


@pytest.mark.parametrize(
    "value", ["480&520", "nan&520", "600&500", "-1&10", "1", "bad&10", "1&inf"]
)
@pytest.mark.asyncio
async def test_rds_values_and_reversed_format(value: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["x-acs-action"] != "DescribeDBInstancePerformance":
            return mock_handler(request)
        data = copy.deepcopy(PERFORMANCE)
        keys = cast(dict[str, JsonValue], data["PerformanceKeys"])
        records = cast(list[dict[str, JsonValue]], keys["PerformanceKey"])
        records[0]["ValueFormat"] = (
            "total_session&active_session" if value == "480&520" else "active_session&total_session"
        )
        records[0]["Values"] = {
            "PerformanceValue": [
                {
                    "Date": "2026-10-01T01:08:00Z",
                    "Value": "520&480" if value == "480&520" else value,
                }
            ]
        }
        return httpx.Response(200, json=data)

    async with HTTPCloudConnector(
        config(("rds",)), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        if value == "480&520":
            result = next(
                r for r in (await reader.get_cloud_resources(QUERY)).resources if r.product == "rds"
            )
            assert (
                result.rds_connections is not None
                and result.rds_connections.active_connections == 480
            )
        else:
            with pytest.raises(CloudResponseError):
                await reader.get_cloud_resources(QUERY)


@pytest.mark.asyncio
async def test_latest_rds_sample_and_out_of_window_is_not_zero() -> None:
    samples: list[JsonObject] = [
        {"Date": "2026-10-01T01:10:00Z", "Value": "500&550"},
        {"Date": "2026-10-01T01:02:00Z", "Value": "10&15"},
        {"Date": "2026-10-01T01:08:00Z", "Value": "480&520"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["x-acs-action"] == "DescribeDBInstancePerformance":
            return httpx.Response(
                200,
                json={
                    **PERFORMANCE,
                    "PerformanceKeys": {
                        "PerformanceKey": [
                            {
                                "Key": "MySQL_Sessions",
                                "ValueFormat": "active_session&total_session",
                                "Values": {"PerformanceValue": samples},
                            }
                        ]
                    },
                },
            )
        return mock_handler(request)

    async with HTTPCloudConnector(
        config(("rds",)), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        observation = (await reader.get_cloud_resources(QUERY)).resources[0].rds_connections
        assert observation is not None and observation.sampled_at == SAMPLE_TIME
        samples[:] = samples[:1]
        observation = (await reader.get_cloud_resources(QUERY)).resources[0].rds_connections
        assert observation is not None and observation.availability == "no_data"
        assert observation.total_connections is None
        samples.clear()
        assert (await reader.get_cloud_resources(QUERY)).resources[0].rds_connections == observation


@pytest.mark.asyncio
async def test_other_rds_engine_explicitly_unsupported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        action = request.headers["x-acs-action"]
        assert action != "DescribeDBInstancePerformance"
        if action == "DescribeDBInstanceAttribute":
            return httpx.Response(
                200,
                json={
                    "Items": {
                        "DBInstanceAttribute": [
                            {
                                "DBInstanceId": "rm-payment",
                                "DBInstanceStatus": "Running",
                                "Engine": "PostgreSQL",
                            }
                        ]
                    }
                },
            )
        return mock_handler(request)

    async with HTTPCloudConnector(
        config(("rds",)), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        observation = (await reader.get_cloud_resources(QUERY)).resources[0].rds_connections
        assert observation is not None and observation.availability == "unsupported_engine"


def test_signature_sts_and_readonly_api_list() -> None:
    key = AlibabaReaderKey(access_key_id=SecretStr("id"), access_key_secret=SecretStr("secret"))
    api = ReadAPI("ecs", "DescribeInstances", "2014-05-26")
    headers = signed_headers(
        key,
        "ecs.invalid",
        api,
        {"RegionId": "cn-hangzhou", "InstanceIds": '["i-1"]'},
        timestamp="2026-10-01T01:00:00Z",
        nonce="nonce",
    )
    assert "x-acs-security-token" not in headers
    # 由官方规范的固定 GET 规范化串独立计算，验证 URL 编码及空请求体摘要。
    assert headers["Authorization"].endswith(
        "Signature=9a041da3551f8183cad1a80e65f8c4a1997aeb9f137e7d9dfa55cba10118d2ee"
    )
    assert all(
        api.action.startswith("Describe") or api.action in {"OnsInstanceBaseInfo", "OnsTopicList"}
        for api in READ_APIS
    )


def test_fake_rejects_dangling_events_and_duplicate_resources() -> None:
    with pytest.raises(ValueError, match="不能重复"):
        FakeCloudConnector(resources=(*sample_resources(), sample_resources()[0]))
    with pytest.raises(ValueError, match="关联已有资源"):
        FakeCloudConnector(resources=(), events=sample_events())


@pytest.mark.parametrize(
    "action,invalid",
    [
        ("DescribeInstances", {"TotalCount": 1, "Instances": {"Instance": []}}),
        ("DescribeInstances", {"TotalCount": True, "Instances": {"Instance": []}}),
        ("DescribeDBInstanceAttribute", {"Items": {"DBInstanceAttribute": []}}),
        ("DescribeInstanceAttribute", {"Instances": {"DBInstanceAttribute": "bad"}}),
        (
            "OnsInstanceBaseInfo",
            {"InstanceBaseInfo": {"InstanceId": "MQ_INST_payment", "InstanceStatus": True}},
        ),
        ("DescribeDomainInfo", {"DomainName": "payment.example.com", "InClean": "false"}),
        (
            "DescribeCdnDomainDetail",
            {"GetDomainDetailModel": {"DomainName": "other.example.com", "DomainStatus": "online"}},
        ),
        ("DescribeDBInstancePerformance", {**PERFORMANCE, "DBInstanceId": "rm-other"}),
        (
            "DescribeDBInstancePerformance",
            {**PERFORMANCE, "PerformanceKeys": {"PerformanceKey": [{"Key": "OtherMetric"}]}},
        ),
        (
            "DescribeDBInstancePerformance",
            {
                **PERFORMANCE,
                "PerformanceKeys": {
                    "PerformanceKey": [{"Key": "MySQL_Sessions", "ValueFormat": "unknown&fields"}]
                },
            },
        ),
        (
            "DescribeSystemEventAttribute",
            {"Code": "200", "Success": 1, "SystemEvents": {"SystemEvent": []}},
        ),
        (
            "DescribeSystemEventAttribute",
            {"Code": "500", "Success": "true", "Message": "mock-secret"},
        ),
        (
            "DescribeSystemEventAttribute",
            {"Code": "200", "Success": "true", "SystemEvents": {"SystemEvent": "bad"}},
        ),
        (
            "DescribeSystemEventAttribute",
            {
                "Code": "200",
                "Success": "true",
                "SystemEvents": {"SystemEvent": [{**EVENT, "Time": True}]},
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_incomplete_or_malformed_native_responses_fail(
    action: str, invalid: JsonObject
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["x-acs-action"] == action:
            return httpx.Response(200, json=invalid)
        return mock_handler(request)

    async with HTTPCloudConnector(
        config(), credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        with pytest.raises(CloudResponseError) as error:
            await reader.get_cloud_resources(QUERY)
        assert "mock-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_undeclared_cloud_action_blocked_before_transport() -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("只读清单外动作不应访问 transport")

    client = CloudHTTP(config(), credentials(), httpx.MockTransport(unexpected))
    try:
        with pytest.raises(CloudError, match="只读清单"):
            await client.read(ReadAPI("ecs", "DeleteInstance", "2014-05-26"), {})
    finally:
        await client.client.aclose()


@pytest.mark.asyncio
async def test_config_mutation_and_invalid_query_cannot_redirect_requests() -> None:
    original = config(("vpc",))
    async with HTTPCloudConnector(
        original, credentials(), transport=httpx.MockTransport(mock_handler)
    ) as reader:
        original.endpoints["vpc"] = "https://evil.invalid/"
        original.services.clear()
        assert (await reader.get_cloud_resources(QUERY)).resources[0].resource_id == "vpc-payment"
        with pytest.raises(ValidationError):
            await reader.get_cloud_resources(QUERY.model_copy(update={"end": SAMPLE_START}))


@pytest.mark.asyncio
async def test_environment_json_config_and_keyless_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CONNECTOR_MODE", "real")
    monkeypatch.setenv("CLOUD_CONFIG", config().model_dump_json())
    monkeypatch.setenv("CONNECTOR_READER_TOKENS", json.dumps({"cloud": KEY_JSON}))
    async with create_cloud_connector(
        Settings(), transport=httpx.MockTransport(mock_handler)
    ) as reader:
        assert len((await reader.get_cloud_resources(QUERY)).resources) == 8
    monkeypatch.setenv("CONNECTOR_MODE", "fake")
    monkeypatch.delenv("CONNECTOR_READER_TOKENS")
    async with create_cloud_connector(Settings()) as reader:
        assert isinstance(reader, FakeCloudConnector) and reader.reader_credentials is None
