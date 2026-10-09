"""Step 12 共享只读契约、标准 API 和失败行为；禁止实际网络。"""

import json
from collections.abc import AsyncIterator
from datetime import UTC
from typing import cast

import httpx2 as httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.config import Settings
from app.connectors.kubernetes.__main__ import main as demo
from app.connectors.kubernetes.client import (
    HTTPKubernetesConnector,
    KubernetesConnector,
    KubernetesError,
    KubernetesHTTPError,
    KubernetesResponseError,
    KubernetesTimeout,
    KubernetesTransportError,
)
from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.kubernetes.factory import create_kubernetes_connector
from app.connectors.kubernetes.fake import FakeKubernetesConnector, sample_snapshot
from app.connectors.kubernetes.models import Deployment, Event, KubernetesSnapshot, Pod
from app.connectors.models import ExecutorCredentials, ReaderCredentials

pytestmark = pytest.mark.usefixtures("forbid_llm_network")

CONFIG = {
    "cluster_name": "ack-fake",
    "base_url": "https://k8s.example.invalid:6443",
    "page_size": 1,
    "max_pages": 10,
    "timeout_seconds": 1.25,
}
READER = ReaderCredentials(connector="kubernetes", token="k8s-reader-mock-secret")


def real_settings(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "APP_ENV": "staging",
            "CONNECTOR_MODE": "real",
            "KUBERNETES_CONFIG": CONFIG,
            "CONNECTOR_READER_TOKENS": {"kubernetes": READER.token},
            **overrides,
        }
    )


def source(snapshot: KubernetesSnapshot) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "k8s.example.invalid" and request.url.port == 6443
        assert request.headers["authorization"] == "Bearer k8s-reader-mock-secret"
        assert request.headers["accept"] == "application/json"
        assert request.extensions["timeout"] == dict(connect=1.25, read=1.25, write=1.25, pool=1.25)
        path = request.url.path.split("/")
        namespace, collection = path[-2:]
        records: tuple[Deployment | Pod | Event, ...]
        if collection == "deployments":
            assert request.url.path == f"/apis/apps/v1/namespaces/{namespace}/deployments"
            records = snapshot.deployments
            api_version, kind = "apps/v1", "DeploymentList"
        elif collection == "pods":
            assert request.url.path == f"/api/v1/namespaces/{namespace}/pods"
            records = snapshot.pods
            api_version, kind = "v1", "PodList"
        else:
            assert collection == "events"
            assert request.url.path == f"/api/v1/namespaces/{namespace}/events"
            assert "labelSelector" not in request.url.params
            records = snapshot.events
            api_version, kind = "v1", "EventList"
        query = request.url.params
        records = tuple(item for item in records if item.metadata.namespace == namespace)
        if "labelSelector" in query:
            key, value = query["labelSelector"].split("=")
            records = tuple(item for item in records if item.metadata.labels.get(key) == value)
        start, limit = int(query.get("continue", "0")), int(query["limit"])
        return httpx.Response(
            200,
            json={
                "apiVersion": api_version,
                "kind": kind,
                "metadata": {
                    "continue": str(start + limit) if start + limit < len(records) else "",
                    "resourceVersion": "100",
                },
                "items": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in records[start : start + limit]
                ],
            },
        )

    return httpx.MockTransport(handle)


def mixed_snapshot() -> KubernetesSnapshot:
    original = sample_snapshot()
    data = original.model_dump(mode="json", by_alias=True)
    pods = data["pods"]
    checkout = json.loads(json.dumps(pods[0]))
    checkout["metadata"].update(
        uid="pod-checkout",
        name="checkout-service-1",
        labels={"app.kubernetes.io/name": "checkout-service"},
    )
    other = json.loads(json.dumps(pods[0]))
    other["metadata"].update(uid="pod-other", namespace="other")
    pods.extend([checkout, other])
    events = data["events"]
    for uid, ref, namespace in [
        ("event-checkout", checkout["metadata"], "payment"),
        ("event-other", other["metadata"], "other"),
        ("event-old-pod", {**pods[2]["metadata"], "uid": "deleted-pod"}, "payment"),
    ]:
        event = json.loads(json.dumps(events[0]))
        event["metadata"].update(uid=uid, name=uid, namespace=namespace)
        event["involvedObject"].update(uid=ref["uid"], name=ref["name"], namespace=namespace)
        events.append(event)
    return KubernetesSnapshot.model_validate_json(json.dumps(data))


@pytest_asyncio.fixture(params=["fake", "http-mock"])
async def reader(request: pytest.FixtureRequest) -> AsyncIterator[KubernetesConnector]:
    snapshot = mixed_snapshot()
    connector = (
        FakeKubernetesConnector(snapshot)
        if request.param == "fake"
        else create_kubernetes_connector(real_settings(), transport=source(snapshot))
    )
    async with connector:
        yield connector


@pytest.mark.asyncio
async def test_shared_contract_and_namespace_service_uid_filters(
    reader: KubernetesConnector,
) -> None:
    assert reader.cluster_name == "ack-fake"
    deployments = await reader.list_deployments("payment", service_name="payment-service")
    assert len(deployments) == 1
    assert deployments[0].spec.replicas == 3 and deployments[0].status.ready_replicas == 2
    pods = await reader.list_pods("payment", service_name="payment-service")
    assert len(pods) == 3
    assert pods[2].status.container_statuses[0].restart_count == 4
    assert len(await reader.list_pods("payment")) == 4
    assert len(await reader.list_pods("other", service_name="payment-service")) == 1
    events = await reader.list_events("payment", service_name="payment-service")
    assert len(events) == 1 and events[0].metadata.uid == "event-payment-backoff"
    assert events[0].last_timestamp is not None and events[0].last_timestamp.tzinfo is UTC
    assert len(await reader.list_events("payment")) == 3
    assert len(await reader.list_events("payment", service_name="checkout-service")) == 1
    assert await reader.list_pods("payment", service_name="missing") == ()
    assert await reader.list_events("payment", service_name="missing") == ()


@pytest.mark.parametrize("method", ["list_pods", "list_deployments", "list_events"])
@pytest.mark.parametrize(
    "namespace,service",
    [
        ("../payment", None),
        ("PAYMENT", None),
        ("", None),
        ("payment", "x,y"),
        ("payment", "x=other"),
        ("payment", ""),
    ],
)
@pytest.mark.asyncio
async def test_invalid_selector_fails_before_request(
    reader: KubernetesConnector, method: str, namespace: str, service: str | None
) -> None:
    with pytest.raises(ValidationError):
        await getattr(reader, method)(namespace, service_name=service)


@pytest.mark.parametrize("method", ["list_pods", "list_deployments", "list_events"])
@pytest.mark.asyncio
async def test_closed_readers_fail(reader: KubernetesConnector, method: str) -> None:
    await reader.aclose()
    await reader.aclose()
    with pytest.raises(KubernetesError, match="已关闭"):
        await getattr(reader, method)("payment")


@pytest.mark.asyncio
async def test_fake_snapshot_isolated_from_mutable_labels() -> None:
    snapshot = sample_snapshot()
    async with FakeKubernetesConnector(snapshot) as connector:
        snapshot.pods[0].metadata.labels.clear()
        pods = await connector.list_pods("payment", service_name="payment-service")
        pods[0].metadata.labels.clear()
        assert len(await connector.list_pods("payment", service_name="payment-service")) == 3


def test_factory_defaults_fake_and_real_has_only_reader() -> None:
    assert isinstance(
        create_kubernetes_connector(Settings(APP_ENV="test")), FakeKubernetesConnector
    )
    connector = create_kubernetes_connector(real_settings(), transport=source(sample_snapshot()))
    assert isinstance(connector, HTTPKubernetesConnector) and connector.reader_credentials == READER
    for instance in (connector, FakeKubernetesConnector()):
        for method in (
            "restart",
            "scale",
            "rollback",
            "execute_action",
            "delete",
            "patch",
            "request",
        ):
            assert not hasattr(instance, method)


@pytest.mark.parametrize("env", ["local", "test"])
def test_real_forbidden_in_local_and_test(env: str) -> None:
    with pytest.raises(ValidationError, match="只允许 fake"):
        real_settings(APP_ENV=env)
    mutated = Settings(APP_ENV=env).model_copy(update={"connector_mode": "real"})
    with pytest.raises(ValidationError, match="只允许 fake"):
        create_kubernetes_connector(mutated)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"KUBERNETES_CONFIG": None}, "KUBERNETES_CONFIG"),
        ({"CONNECTOR_READER_TOKENS": {}}, "只读凭证"),
    ],
)
def test_real_requires_explicit_config_and_identity(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        create_kubernetes_connector(real_settings(**overrides))


@pytest.mark.parametrize(
    "url",
    [
        "http://k8s.invalid",
        "https://user:pass@k8s.invalid",
        "https://k8s.invalid/api",
        "https://k8s.invalid?x=1",
        "https://k8s.invalid#x",
        "https://k8s.invalid:0",
        "https://k8s.invalid/../",
        "https://k8s.invalid\\api",
    ],
)
def test_invalid_base_url(url: str) -> None:
    with pytest.raises(ValidationError):
        KubernetesConfig(cluster_name="mock", base_url=url)


@pytest.mark.parametrize(
    "key", ["", "a=b", "a,b", "/app", "a/b/c", "Upper.invalid/app", "app/", "a" * 64]
)
def test_invalid_label_key(key: str) -> None:
    with pytest.raises(ValidationError):
        KubernetesConfig(**CONFIG, service_label_key=key)


def test_config_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("KUBERNETES_CONFIG", json.dumps(CONFIG))
    assert Settings().require_kubernetes_config().cluster_name == "ack-fake"


def test_reject_executor_and_wrong_connector_credentials() -> None:
    config = KubernetesConfig(**CONFIG)
    with pytest.raises(TypeError, match="ReaderCredentials"):
        HTTPKubernetesConnector(
            config,
            cast(ReaderCredentials, ExecutorCredentials(connector="kubernetes", token="executor")),
        )
    with pytest.raises(ValueError, match="kubernetes"):
        HTTPKubernetesConnector(config, ReaderCredentials(connector="other", token="reader"))
    with pytest.raises(ValueError, match="控制字符"):
        HTTPKubernetesConnector(
            config, ReaderCredentials(connector="kubernetes", token="reader\r\n")
        )
    with pytest.raises(ValueError, match="CA 证书"):
        HTTPKubernetesConnector(KubernetesConfig(**CONFIG, ca_cert_pem="invalid"), READER)


@pytest.mark.parametrize("status", [301, 401, 403, 404, 410, 429, 500])
@pytest.mark.asyncio
async def test_http_errors_redacted_and_no_redirect_retry(status: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, text="k8s-reader-mock-secret", headers={"Location": "https://other.invalid"}
        )

    async with create_kubernetes_connector(
        real_settings(), transport=httpx.MockTransport(handle)
    ) as connector:
        with pytest.raises(KubernetesHTTPError) as caught:
            await connector.list_pods("payment")
    assert caught.value.status_code == status and len(requests) == 1
    assert "secret" not in str(caught.value) and "invalid" not in str(caught.value)


@pytest.mark.parametrize(
    "error,expected",
    [(httpx.ReadTimeout, KubernetesTimeout), (httpx.ConnectError, KubernetesTransportError)],
)
@pytest.mark.asyncio
async def test_transport_errors_redacted(
    error: type[httpx.RequestError], expected: type[KubernetesError]
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error("k8s-reader-mock-secret", request=request)

    async with create_kubernetes_connector(
        real_settings(), transport=httpx.MockTransport(handle)
    ) as connector:
        with pytest.raises(expected) as caught:
            await connector.list_pods("payment")
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "fault",
    [
        "json",
        "kind",
        "version",
        "missing",
        "namespace",
        "selector",
        "negative",
        "duplicate",
        "cursor-loop",
        "version-change",
        "max-pages",
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_incomplete_response_is_rejected(fault: str) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        pod = sample_snapshot().pods[0].model_dump(mode="json", by_alias=True)
        if fault == "json":
            return httpx.Response(200, text="invalid-json-secret")
        page_metadata = {"resourceVersion": "100"}
        payload: dict[str, object] = {
            "apiVersion": "v1",
            "kind": "PodList",
            "metadata": page_metadata,
            "items": [pod],
        }
        if fault == "kind":
            payload["kind"] = "SecretList"
        if fault == "version":
            payload["apiVersion"] = "apps/v1"
        if fault == "missing":
            del pod["metadata"]["uid"]
        if fault == "namespace":
            pod["metadata"]["namespace"] = "other"
        if fault == "selector":
            pod["metadata"]["labels"] = {"app.kubernetes.io/name": "other"}
        if fault == "negative":
            pod["status"]["containerStatuses"][0]["restartCount"] = -1
        if fault == "duplicate":
            payload["items"] = [pod, pod]
        if fault in {"cursor-loop", "version-change", "max-pages"}:
            payload["items"] = []
            page_metadata["continue"] = "same" if fault == "cursor-loop" else str(calls)
        if fault == "version-change" and calls > 1:
            page_metadata["resourceVersion"] = "101"
        return httpx.Response(200, json=payload)

    async with create_kubernetes_connector(
        real_settings(), transport=httpx.MockTransport(handle)
    ) as connector:
        with pytest.raises(KubernetesResponseError):
            await connector.list_pods("payment", service_name="payment-service")


def test_minimal_snapshot_discards_secrets_and_preserves_unknown_status() -> None:
    pod = sample_snapshot().pods[0].model_dump(mode="json", by_alias=True)
    pod["spec"]["containers"][0]["env"] = [{"name": "PASSWORD", "value": "sensitive"}]
    del pod["status"]
    parsed = Pod.model_validate_json(json.dumps(pod))
    assert parsed.status.phase is None
    assert "sensitive" not in parsed.model_dump_json()
    deployment = sample_snapshot().deployments[0].model_dump(mode="json", by_alias=True)
    deployment["status"] = {}
    assert Deployment.model_validate_json(json.dumps(deployment)).status.observed_generation is None


@pytest.mark.parametrize("fault", ["naive", "order", "namespace", "duplicate"])
def test_snapshot_rejects_invalid_events_and_duplicates(fault: str) -> None:
    data = sample_snapshot().model_dump(mode="json", by_alias=True)
    event = data["events"][0]
    if fault == "naive":
        event["lastTimestamp"] = "2026-10-01T01:05:00"
    if fault == "order":
        event["lastTimestamp"] = "2026-10-01T00:00:00Z"
    if fault == "namespace":
        event["involvedObject"]["namespace"] = "other"
    if fault == "duplicate":
        data["pods"].append(data["pods"][0])
    with pytest.raises(ValidationError):
        KubernetesSnapshot.model_validate_json(json.dumps(data))


@pytest.mark.asyncio
async def test_demo_ignores_host_production_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CONNECTOR_MODE", "real")
    monkeypatch.setenv("KUBERNETES_CONFIG", json.dumps(CONFIG))
    monkeypatch.setenv("CONNECTOR_READER_TOKENS", '{"kubernetes":"real-secret"}')
    await demo()
    output = capsys.readouterr().out
    assert "Step 12 Fake 样例验收通过" in output and "BackOff" in output
    assert "real-secret" not in output and "k8s.example.invalid" not in output


@pytest.mark.parametrize("mode", ["fake", "http-mock"])
@pytest.mark.asyncio
async def test_custom_service_label_key(mode: str) -> None:
    data = sample_snapshot().model_dump(mode="json", by_alias=True)
    for item in [*data["deployments"], *data["pods"]]:
        item["metadata"]["labels"] = {"app": "payment-service"}
    snapshot = KubernetesSnapshot.model_validate_json(json.dumps(data))
    connector = (
        FakeKubernetesConnector(snapshot, service_label_key="app")
        if mode == "fake"
        else create_kubernetes_connector(
            real_settings(KUBERNETES_CONFIG={**CONFIG, "service_label_key": "app"}),
            transport=source(snapshot),
        )
    )
    async with connector:
        assert len(await connector.list_deployments("payment", service_name="payment-service")) == 1
        assert len(await connector.list_pods("payment", service_name="payment-service")) == 3
        assert len(await connector.list_events("payment", service_name="payment-service")) == 1


@pytest.mark.asyncio
async def test_empty_page_with_continue_is_not_end() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["labelSelector"] == "app.kubernetes.io/name=payment-service"
        items = (
            []
            if len(requests) == 1
            else [sample_snapshot().pods[0].model_dump(mode="json", by_alias=True)]
        )
        return httpx.Response(
            200,
            json={
                "apiVersion": "v1",
                "kind": "PodList",
                "items": items,
                "metadata": {
                    "continue": "opaque+/=" if len(requests) == 1 else "",
                    "resourceVersion": "100",
                },
            },
        )

    async with create_kubernetes_connector(
        real_settings(), transport=httpx.MockTransport(handle)
    ) as connector:
        assert len(await connector.list_pods("payment", service_name="payment-service")) == 1
    assert len(requests) == 2 and requests[1].url.params["continue"] == "opaque+/="


@pytest.mark.asyncio
async def test_cluster_object_event_and_non_utc_time_are_valid() -> None:
    data = sample_snapshot().model_dump(mode="json", by_alias=True)
    event = data["events"][0]
    event["involvedObject"] = {"kind": "Node", "name": "fake-node-1", "namespace": ""}
    event["lastTimestamp"] = "2026-10-01T09:05:00+08:00"
    snapshot = KubernetesSnapshot.model_validate_json(json.dumps(data))
    async with FakeKubernetesConnector(snapshot) as connector:
        events = await connector.list_events("payment")
        assert len(events) == 1 and events[0].involved_object.namespace is None
        assert events[0].last_timestamp is not None and events[0].last_timestamp.hour == 1
        assert await connector.list_events("payment", service_name="payment-service") == ()
