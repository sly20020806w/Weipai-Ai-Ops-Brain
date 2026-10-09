"""Step 21：离线验签、归一化、HTTP 适配与 K8s Watch 协议。"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx2 as httpx
import pytest
from fastapi import Request
from pydantic import SecretStr, ValidationError

from app.api.events import get_event_gateway
from app.api.main import create_app
from app.config import Settings
from app.connectors.kubernetes.client import HTTPKubernetesConnector
from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.kubernetes.fake import FakeKubernetesConnector, sample_snapshot
from app.connectors.kubernetes.watch import WatchResponseError
from app.connectors.models import ReaderCredentials
from app.tasks.states import TaskSource
from app.triggers.config import TriggerConfig
from app.triggers.gateway import EventGateway
from app.triggers.normalization import (
    PayloadError,
    SignatureError,
    normalize_kubernetes,
    normalize_webhook,
    verify_signature,
)
from app.triggers.schemas import EventReceipt, NormalizedEvent

SECRET = "offline-test-signature-key-32-characters"
pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def alert_body(
    *,
    service: str = "payment-service",
    starts: str = "2026-10-01T01:30:00Z",
    status: str = "firing",
) -> bytes:
    return json.dumps(
        {
            "version": "4",
            "alerts": [
                {
                    "status": status,
                    "labels": {"alertname": "Payment5xxHigh", "service": service},
                    "startsAt": starts,
                }
            ],
        }
    ).encode()


def signed(body: bytes, *, timestamp: str | None = None, secret: str = SECRET) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-Ops-Timestamp": timestamp,
        "X-Ops-Signature": f"sha256={signature}",
        "Content-Type": "application/json",
    }


def test_signature_and_secret_not_exposed() -> None:
    config = TriggerConfig(webhook_secrets={"prometheus": SecretStr(SECRET)})
    body = alert_body()
    headers = signed(body, timestamp="1000")
    verify_signature(config, "prometheus", body, "1000", headers["X-Ops-Signature"], now=1000)
    assert SECRET not in repr(config)


@pytest.mark.parametrize(
    "change", ["secret", "body", "past", "future", "missing", "unicode", "origin"]
)
def test_invalid_signature_rejected(change: str) -> None:
    config = TriggerConfig(webhook_secrets={"prometheus": SecretStr(SECRET)})
    body, timestamp, origin = alert_body(), "1000", "prometheus"
    signature = signed(body, timestamp=timestamp)["X-Ops-Signature"]
    if change == "secret":
        signature = signed(body, timestamp=timestamp, secret="wrong")["X-Ops-Signature"]
    if change == "body":
        body += b" "
    if change in {"past", "future"}:
        timestamp = "1" if change == "past" else "9999"
        signature = signed(body, timestamp=timestamp)["X-Ops-Signature"]
    if change == "missing":
        signature = ""
    if change == "unicode":
        timestamp = "١٠٠٠"
    if change == "origin":
        origin = "git"
    with pytest.raises(SignatureError):
        verify_signature(config, origin, body, timestamp, signature, now=1000)


def test_alert_dedup_identity_utc_and_recurrence() -> None:
    first = normalize_webhook("prometheus", alert_body())[0]
    value = json.loads(alert_body())
    value["alerts"][0]["labels"] = {"service": "payment-service", "alertname": "Payment5xxHigh"}
    value["alerts"][0]["fingerprint"] = "untrusted-sender-value"
    value["alerts"][0]["annotations"] = {"secret": "discarded"}
    value["alerts"][0]["startsAt"] = "2026-10-01T09:30:00+08:00"
    second = normalize_webhook("prometheus", json.dumps(value).encode())[0]
    assert first.fingerprint == second.fingerprint
    assert first.source is TaskSource.ALERT and first.occurred_at.tzinfo is UTC
    assert "discarded" not in second.model_dump_json()
    assert (
        first.fingerprint
        != normalize_webhook("prometheus", alert_body(starts="2026-10-02T01:30:00Z"))[0].fingerprint
    )
    assert normalize_webhook("prometheus", alert_body(status="resolved")) == []


@pytest.mark.parametrize(
    "origin,source",
    [
        ("ops_platform", "Ticket"),
        ("git", "Release"),
        ("ci", "Release"),
        ("argocd", "Release"),
        ("config_center", "Release"),
        ("cloud", "Alert"),
        ("manual", "Human"),
    ],
)
def test_multiple_source_normalization(origin: str, source: str) -> None:
    from typing import cast

    from app.triggers.schemas import EventOrigin

    body = json.dumps(
        {
            "external_id": "event-1",
            "service_name": "payment-service",
            "title": "事件样例",
            "occurred_at": "2026-10-01T09:30:00+08:00",
            "source": source,
        }
    ).encode()
    event = normalize_webhook(cast(EventOrigin, origin), body)[0]
    assert event.source.value == source and event.occurred_at == datetime(
        2026, 10, 1, 1, 30, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"{}",
        alert_body(service=""),
        alert_body(starts="2026-10-01T01:30:00"),
        json.dumps({"alerts": [], "truncatedAlerts": 1}).encode(),
    ],
)
def test_invalid_batch_rejected_without_partial_accept(body: bytes) -> None:
    with pytest.raises(PayloadError):
        normalize_webhook("prometheus", body)


def test_generic_source_cannot_forge_task_source() -> None:
    with pytest.raises(PayloadError):
        normalize_webhook(
            "manual",
            b'{"external_id":"1","service_name":"payment-service","title":"test","occurred_at":"2026-10-01T01:00:00Z","source":"Schedule"}',
        )


def test_config_and_event_validation() -> None:
    with pytest.raises(ValidationError):
        TriggerConfig(webhook_secrets={"prometheus": SecretStr("short")})
    with pytest.raises(ValidationError):
        TriggerConfig(watcher_namespaces=("payment", "payment"))
    with pytest.raises(ValidationError):
        TriggerConfig(watcher_namespaces=("bad/path",))
    with pytest.raises(ValidationError):
        NormalizedEvent(
            origin="manual",
            source=TaskSource.HUMAN,
            external_id=" ",
            service_name="payment-service",
            title="样例",
            occurred_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_fake_warning_and_updated_count_same_event_identity() -> None:
    async with FakeKubernetesConnector() as connector:
        batch = await connector.watch_events("payment")
    event = batch.events[0]
    first = normalize_kubernetes("ack-fake", event)
    second = normalize_kubernetes(
        "ack-fake", event.model_copy(update={"count": 10, "last_timestamp": datetime.now(UTC)})
    )
    assert first is not None and second is not None
    assert first.fingerprint == second.fingerprint and first.occurred_at == second.occurred_at
    mapped = normalize_kubernetes("ack-fake", event, service_name="payment-service")
    assert mapped is not None and mapped.fingerprint == first.fingerprint
    assert normalize_kubernetes("ack-fake", event.model_copy(update={"type": "Normal"})) is None
    with pytest.raises(PayloadError):
        normalize_kubernetes(
            "ack-fake", event.model_copy(update={"first_timestamp": None, "last_timestamp": None})
        )


class FakeGateway(EventGateway):
    def __init__(self) -> None:
        self.seen: list[NormalizedEvent] = []

    async def submit(self, events: list[NormalizedEvent]) -> list[EventReceipt]:
        self.seen.extend(events)
        return [EventReceipt(str(uuid4()), str(uuid4()), "fake-workflow", False)]


@pytest.mark.asyncio
async def test_http_signature_before_io_202_and_validation() -> None:
    settings = Settings(
        APP_ENV="test",
        TRIGGER_CONFIG=TriggerConfig(
            webhook_secrets={"prometheus": SecretStr(SECRET)}, max_body_bytes=1024
        ),
    )
    app = create_app(settings)
    gateway = FakeGateway()

    async def override(request: Request) -> EventGateway:
        return gateway

    app.dependency_overrides[get_event_gateway] = override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = alert_body()
        response = await client.post("/webhooks/prometheus", content=body, headers=signed(body))
        assert response.status_code == 202 and len(response.json()["events"]) == 1
        assert len(gateway.seen) == 1
        bad = await client.post(
            "/webhooks/prometheus", content=body, headers=signed(body, secret="wrong")
        )
        assert bad.status_code == 401 and len(gateway.seen) == 1
        for invalid in (b"{}", b"bad-json"):
            response = await client.post(
                "/webhooks/prometheus", content=invalid, headers=signed(invalid)
            )
            assert response.status_code == 422
        response = await client.post("/webhooks/prometheus", content=b"x" * 1025)
        assert response.status_code == 413
        resolved = alert_body(status="resolved")
        response = await client.post(
            "/webhooks/prometheus", content=resolved, headers=signed(resolved)
        )
        assert response.status_code == 202 and response.json() == {"events": []}
    assert len(gateway.seen) == 1


@pytest.mark.asyncio
async def test_invalid_signature_does_not_connect_temporal_and_missing_db_503() -> None:
    app = create_app(
        Settings(
            APP_ENV="test",
            TRIGGER_CONFIG=TriggerConfig(webhook_secrets={"prometheus": SecretStr(SECRET)}),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = alert_body()
        assert (await client.post("/webhooks/prometheus", content=body)).status_code == 401
        assert (
            await client.post("/webhooks/prometheus", content=body, headers=signed(body))
        ).status_code == 503


def real_connector(handler: object) -> HTTPKubernetesConnector:
    from collections.abc import Callable
    from typing import cast

    return HTTPKubernetesConnector(
        KubernetesConfig(cluster_name="ack-mock", base_url="https://k8s.example.invalid"),
        ReaderCredentials(connector="kubernetes", token=SecretStr("offline-reader")),
        transport=httpx.MockTransport(cast(Callable[[httpx.Request], httpx.Response], handler)),
    )


@pytest.mark.asyncio
async def test_http_list_watch_bookmark_and_expired_resource_version() -> None:
    event = sample_snapshot().events[0].model_dump(mode="json")
    event["metadata"]["resourceVersion"] = "11"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "watch" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "apiVersion": "v1",
                    "kind": "EventList",
                    "metadata": {"resourceVersion": "10"},
                    "items": [event],
                },
            )
        if request.url.params["resourceVersion"] == "old":
            return httpx.Response(410)
        lines = [
            {"type": "ADDED", "object": event},
            {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "12"}}},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines))

    async with real_connector(handler) as connector:
        initial = await connector.watch_events("payment")
        batch = await connector.watch_events(
            "payment", resource_version=initial.resource_version, timeout_seconds=2
        )
        expired = await connector.watch_events("payment", resource_version="old")
    assert len(initial.events) == len(batch.events) == 1 and batch.resource_version == "12"
    assert expired.resource_version == "" and expired.events == ()
    assert (
        requests[1].url.params["watch"] == "true"
        and requests[1].url.params["timeoutSeconds"] == "2"
    )
    assert all(
        request.method == "GET"
        and str(request.url).startswith(
            "https://k8s.example.invalid/api/v1/namespaces/payment/events"
        )
        for request in requests
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["namespace", "type", "malformed", "expired", "error", "deleted"]
)
async def test_watch_error_and_filtered_stream(change: str) -> None:
    event = sample_snapshot().events[0].model_dump(mode="json")
    event["metadata"]["resourceVersion"] = "11"
    kind = "ADDED"
    if change == "namespace":
        event["metadata"]["namespace"] = "other"
    if change == "type":
        kind = "UNKNOWN"
    if change == "deleted":
        kind = "DELETED"
    if change in {"expired", "error"}:
        kind, event = "ERROR", {"code": 410 if change == "expired" else 500}
    content = "bad-json" if change == "malformed" else json.dumps({"type": kind, "object": event})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    async with real_connector(handler) as connector:
        if change in {"expired", "deleted"}:
            result = await connector.watch_events("payment", resource_version="10")
            assert result.events == ()
            assert result.resource_version == ("" if change == "expired" else "11")
        else:
            with pytest.raises(WatchResponseError):
                await connector.watch_events("payment", resource_version="10")
