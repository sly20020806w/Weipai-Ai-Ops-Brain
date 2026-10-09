"""Step 18 离线验收：真实协议仅 HTTP mock；Fake 发现禁止实际网络。"""

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Literal
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from pydantic import SecretStr, ValidationError
from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy

from app.config import Settings
from app.connectors.changes.base import ChangesResponseError
from app.connectors.changes.config import GitConfig
from app.connectors.changes.git import HTTPGitConnector
from app.connectors.cloud.base import CloudResponseError
from app.connectors.cloud.client import HTTPCloudConnector
from app.connectors.kubernetes.client import HTTPKubernetesConnector, KubernetesResponseError
from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.models import ReaderCredentials
from app.connectors.observability.fake import FakeARMSConnector, discovery_traces
from app.graph.discovery.config import DiscoveryConfig
from app.graph.discovery.models import NodeRef, Relation
from app.graph.discovery.schedule import discovery_schedule, ensure_discovery_schedule
from app.graph.discovery.sources import configured_sources, node
from app.tools.graph import ContextQuery, DependencyQuery
from tests.test_cloud import config as cloud_config
from tests.test_cloud import credentials as cloud_credentials

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
NOW = datetime(2026, 10, 6, 5, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fake_discovers_actual_relationships_and_scopes() -> None:
    async with AsyncExitStack() as stack:
        sources = await configured_sources(stack, Settings(APP_ENV="test"), NOW)
        snapshot = await sources.collect(NOW, 900)
        assert {n.kind for n in snapshot.nodes} >= {
            "service",
            "business",
            "owner",
            "repository",
            "version",
            "cluster",
            "pod",
            "database",
            "cache",
            "topic",
        }
        assert {n.name for n in snapshot.nodes} >= {
            "weipai/payment-service",
            "v2.3.7",
            "ack-fake",
            "rm-payment",
            "r-payment",
            "payment-events",
            "payment-db",
        }
        calls = {
            (e.origin.external_id, e.target.external_id)
            for e in snapshot.relations
            if e.relation == "calls"
        }
        assert calls == {("checkout-service", "payment-service"), ("payment-service", "payment-db")}
        assert all(
            e.source and 0 < e.confidence <= 1 and e.observed_at.tzinfo is UTC
            for e in snapshot.relations
        )
        assert len(
            {(e.origin.key, e.target.key, e.relation, e.source) for e in snapshot.relations}
        ) == len(snapshot.relations)
        # 一个共享 MQ 实例的 Topic 归属不能被夸大为服务一定发布/订阅。
        topic = next(e for e in snapshot.relations if e.target.kind == "topic")
        assert topic.origin.kind == "cloud_mq" and topic.relation == "contains_topic"
        assert snapshot.missing_bindings == ("cloud:checkout-service", "git:checkout-service")
        assert next(n for n in snapshot.nodes if n.external_id == "payment-service").name != (
            "payment-service"
        )
    with pytest.raises(RuntimeError, match="关闭"):
        await sources.git.get_repository("payment-service")
    with pytest.raises(RuntimeError, match="关闭"):
        await sources.k8s.list_namespaces()
    with pytest.raises(RuntimeError, match="关闭"):
        await sources.cloud.list_topics("payment-service")


@pytest.mark.asyncio
async def test_old_trace_not_refreshed_or_inferred_from_missing_parent() -> None:
    async with AsyncExitStack() as stack:
        sources = await configured_sources(stack, Settings(APP_ENV="test"), NOW)
        old = discovery_traces(NOW - timedelta(hours=1))
        sources.arms = await stack.enter_async_context(FakeARMSConnector(old))
        assert not any(e.relation == "calls" for e in (await sources.collect(NOW, 900)).relations)
        trace = discovery_traces(NOW)[0]
        sources.arms = await stack.enter_async_context(
            FakeARMSConnector(
                (
                    trace.model_copy(
                        update={"spans": tuple(s for s in trace.spans if s.span_id == "db")}
                    ),
                )
            )
        )
        assert not any(e.relation == "calls" for e in (await sources.collect(NOW, 900)).relations)


@pytest.mark.parametrize(
    "field,value",
    [
        ("interval_seconds", 0),
        ("lookback_seconds", 86401),
        ("activity_timeout_seconds", 0),
        ("activity_max_attempts", 11),
        ("schedule_id", "../bad"),
    ],
)
def test_invalid_config_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        DiscoveryConfig.model_validate({field: value})


def test_schedule_is_utc_nonoverlapping_and_bounded() -> None:
    schedule = discovery_schedule(DiscoveryConfig(interval_seconds=60), "queue")
    assert schedule.spec.time_zone_name == "UTC"
    assert schedule.spec.intervals[0].every == timedelta(seconds=60)
    assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
    assert schedule.policy.pause_on_failure
    assert schedule.policy.catchup_window == timedelta(seconds=60)
    assert schedule.action.workflow == "DiscoveryWorkflow"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_schedule_registration_idempotent_and_does_not_hide_other_errors() -> None:
    client = AsyncMock()
    config = DiscoveryConfig()
    assert await ensure_discovery_schedule(client, config, "queue")
    client.create_schedule.side_effect = ScheduleAlreadyRunningError()
    assert not await ensure_discovery_schedule(client, config, "queue")
    client.create_schedule.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        await ensure_discovery_schedule(client, config, "queue")


@pytest.mark.parametrize("value", [0, 5, True, "2"])
def test_queries_bound_hops(value: object) -> None:
    for model in (ContextQuery, DependencyQuery):
        with pytest.raises(ValidationError):
            model.model_validate({"service_name": "payment-service", "hops": value})


@pytest.mark.parametrize(
    "value", [{"source": ""}, {"confidence": 1.1}, {"observed_at": datetime(2026, 1, 1)}]
)
def test_relation_requires_provenance_and_utc(value: dict[str, object]) -> None:
    origin = NodeRef(kind="service", external_id="payment-service", name="payment-service")
    with pytest.raises(ValidationError):
        Relation.model_validate(
            dict(
                origin=origin,
                target=origin,
                relation="calls",
                source="arms",
                confidence=0.9,
                observed_at=NOW,
            )
            | value
        )


def test_long_identifiers_have_stable_scoped_hash() -> None:
    first = node("image", "a" * 600)
    assert len(first.external_id) <= 300
    assert first == node("image", "a" * 600)
    assert first.external_id != node("image", "b" + "a" * 599).external_id


@pytest.mark.parametrize("provider", ["gitlab", "github"])
@pytest.mark.asyncio
async def test_repository_reads_fixed_endpoint_and_validates_binding(
    provider: Literal["gitlab", "github"],
) -> None:
    record = {
        "id": 101,
        "path_with_namespace": "weipai/payment-service",
        "full_name": "weipai/payment-service",
        "token": "must-not-copy",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if provider == "gitlab":
            assert request.url.raw_path == b"/api/projects/weipai%2Fpayment-service"
        else:
            assert request.url.path == "/api/repos/weipai/payment-service"
        return httpx.Response(200, json=record)

    async with HTTPGitConnector(
        GitConfig(
            base_url="https://git.invalid/api",
            provider=provider,
            services={"payment-service": "weipai/payment-service"},
        ),
        ReaderCredentials(connector="git", token=SecretStr("mock-token")),
        transport=httpx.MockTransport(handler),
    ) as reader:
        result = await reader.get_repository("payment-service")
        assert result.repository_id == "101" and "must-not-copy" not in result.model_dump_json()
        record["path_with_namespace"] = record["full_name"] = "other/repository"
        with pytest.raises(ChangesResponseError, match="未绑定"):
            await reader.get_repository("payment-service")


@pytest.mark.asyncio
async def test_namespace_pagination_and_snapshot_consistency() -> None:
    records = [
        dict(apiVersion="v1", kind="Namespace", metadata=dict(uid="n1", name="payment")),
        dict(apiVersion="v1", kind="Namespace", metadata=dict(uid="n2", name="commerce")),
    ]
    broken = False

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/api/v1/namespaces"
        assert "labelSelector" not in request.url.params
        index = 1 if request.url.params.get("continue") else 0
        return httpx.Response(
            200,
            json=dict(
                apiVersion="v1",
                kind="NamespaceList",
                metadata=dict(
                    resourceVersion="different" if index and broken else "5",
                    **{"continue": "next" if not index else ""},
                ),
                items=[records[index]],
            ),
        )

    async with HTTPKubernetesConnector(
        KubernetesConfig(cluster_name="mock", base_url="https://k8s.invalid"),
        ReaderCredentials(connector="kubernetes", token=SecretStr("mock-token")),
        transport=httpx.MockTransport(handler),
    ) as reader:
        assert await reader.list_namespaces() == ("payment", "commerce")
        broken = True
        with pytest.raises(KubernetesResponseError, match="版本"):
            await reader.list_namespaces()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["wrong_instance", "duplicate", "missing_data"])
async def test_topic_read_only_binding_and_invalid_results(broken: str) -> None:
    data: dict[str, object] = {
        "Data": {
            "PublishInfoDo": [
                {
                    "InstanceId": "MQ_INST_payment",
                    "Topic": "payment-events",
                    "Remark": "must-not-copy",
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.headers["x-acs-action"] == "OnsTopicList"
        assert request.url.params["InstanceId"] == "MQ_INST_payment"
        assert request.url.params["RegionId"] == "cn-hangzhou"
        return httpx.Response(200, json=data)

    async with HTTPCloudConnector(
        cloud_config(("mq",)), cloud_credentials(), transport=httpx.MockTransport(handler)
    ) as reader:
        result = await reader.list_topics("payment-service")
        assert result[0].name == "payment-events" and "must-not-copy" not in repr(result)
        data = (
            {"Data": {"PublishInfoDo": [{"InstanceId": "other", "Topic": "payment-events"}]}}
            if broken == "wrong_instance"
            else {
                "Data": {
                    "PublishInfoDo": [{"InstanceId": "MQ_INST_payment", "Topic": "payment-events"}]
                    * 2
                }
            }
            if broken == "duplicate"
            else {}
        )
        with pytest.raises(CloudResponseError):
            await reader.list_topics("payment-service")
