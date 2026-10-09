"""可注入快照的离线 ACK 样例，绝不读取真实集群配置。"""

import asyncio
import json
from datetime import UTC, datetime

from app.connectors.kubernetes.client import (
    KubernetesConnector,
    KubernetesError,
    checked_query,
    service_events,
)
from app.connectors.kubernetes.config import KubernetesConfig
from app.connectors.kubernetes.models import (
    Deployment,
    Event,
    KubernetesSnapshot,
    ObjectMeta,
    ObjectReference,
    Pod,
)
from app.connectors.kubernetes.watch import EventWatchBatch


def sample_snapshot() -> KubernetesSnapshot:
    label = {"app.kubernetes.io/name": "payment-service"}
    pods = [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "uid": f"pod-payment-{i}",
                "name": f"payment-service-7d9c-{i}",
                "namespace": "payment",
                "labels": label,
            },
            "spec": {
                "nodeName": "fake-node-1",
                "containers": [
                    {"name": "payment", "image": "registry.example.invalid/payment:v2.3.7"}
                ],
            },
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True" if i < 3 else "False"}],
                "containerStatuses": [
                    {"name": "payment", "ready": i < 3, "restartCount": 0 if i < 3 else 4}
                ],
            },
        }
        for i in range(1, 4)
    ]
    return KubernetesSnapshot.model_validate_json(
        json.dumps(
            {
                "deployments": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {
                            "uid": "deployment-payment",
                            "name": "payment-service",
                            "namespace": "payment",
                            "labels": label,
                            "generation": 7,
                        },
                        "spec": {"replicas": 3},
                        "status": {
                            "replicas": 3,
                            "readyReplicas": 2,
                            "availableReplicas": 2,
                            "updatedReplicas": 3,
                            "observedGeneration": 7,
                            "conditions": [{"type": "Available", "status": "True"}],
                        },
                    }
                ],
                "pods": pods,
                "events": [
                    {
                        "apiVersion": "v1",
                        "kind": "Event",
                        "metadata": {
                            "uid": "event-payment-backoff",
                            "name": "payment-service.backoff",
                            "namespace": "payment",
                        },
                        "involvedObject": {
                            "uid": "pod-payment-3",
                            "kind": "Pod",
                            "name": "payment-service-7d9c-3",
                            "namespace": "payment",
                        },
                        "type": "Warning",
                        "reason": "BackOff",
                        "message": "样例：支付容器重启退避",
                        "count": 4,
                        "firstTimestamp": "2026-10-01T01:00:00Z",
                        "lastTimestamp": "2026-10-01T01:05:00Z",
                    }
                ],
            }
        )
    )


class FakeKubernetesConnector(KubernetesConnector):
    async def watch_events(
        self, namespace: str, *, resource_version: str = "", timeout_seconds: int = 20
    ) -> EventWatchBatch:
        self._check(namespace, None)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError("Watch 超时必须为 1–60 秒")
        if resource_version != "fake-1":
            return EventWatchBatch(await self.list_events(namespace), "fake-1")
        await asyncio.sleep(timeout_seconds)
        return EventWatchBatch((), "fake-1")

    def __init__(
        self,
        snapshot: KubernetesSnapshot | None = None,
        *,
        cluster_name: str = "ack-fake",
        service_label_key: str = "app.kubernetes.io/name",
    ) -> None:
        super().__init__()
        self._config = KubernetesConfig(
            cluster_name=cluster_name,
            service_label_key=service_label_key,
            base_url="https://fake.example.invalid",
        )
        self._snapshot = KubernetesSnapshot.model_validate(
            snapshot if snapshot is not None else sample_snapshot()
        ).model_copy(deep=True)
        self._closed = False

    @property
    def cluster_name(self) -> str:
        return self._config.cluster_name

    async def aclose(self) -> None:
        self._closed = True

    async def list_namespaces(self) -> tuple[str, ...]:
        if self._closed:
            raise KubernetesError("Kubernetes Connector 已关闭")
        objects: tuple[Deployment | Pod | Event, ...] = (
            *self._snapshot.deployments,
            *self._snapshot.pods,
            *self._snapshot.events,
        )
        return tuple(sorted({item.metadata.namespace for item in objects}))

    def _check(self, namespace: str, service_name: str | None) -> None:
        if self._closed:
            raise KubernetesError("Kubernetes Connector 已关闭")
        checked_query(namespace, service_name)

    async def list_deployments(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Deployment, ...]:
        self._check(namespace, service_name)
        return tuple(
            item.model_copy(deep=True)
            for item in self._snapshot.deployments
            if item.metadata.namespace == namespace
            and (
                service_name is None
                or item.metadata.labels.get(self._config.service_label_key) == service_name
            )
        )

    async def list_pods(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Pod, ...]:
        self._check(namespace, service_name)
        return tuple(
            item.model_copy(deep=True)
            for item in self._snapshot.pods
            if item.metadata.namespace == namespace
            and (
                service_name is None
                or item.metadata.labels.get(self._config.service_label_key) == service_name
            )
        )

    async def list_events(
        self, namespace: str, *, service_name: str | None = None
    ) -> tuple[Event, ...]:
        self._check(namespace, service_name)
        events = tuple(
            item.model_copy(deep=True)
            for item in self._snapshot.events
            if item.metadata.namespace == namespace
        )
        if service_name is None:
            return events
        return service_events(
            events,
            await self.list_deployments(namespace, service_name=service_name),
            await self.list_pods(namespace, service_name=service_name),
        )


def timeline_snapshot() -> KubernetesSnapshot:
    """Step 19 固定 UTC 历史样例，不随采集时钟重写发生时间。"""
    snapshot = sample_snapshot()
    events = tuple(
        Event(
            api_version="v1",
            kind="Event",
            metadata=ObjectMeta(
                uid=f"timeline-{reason}", name=f"payment.{minute}", namespace="payment"
            ),
            involved_object=ObjectReference(
                uid=identity, kind=kind, name=name, namespace="payment"
            ),
            type="Normal",
            reason=reason,
            first_timestamp=datetime(2026, 10, 1, 1, minute, tzinfo=UTC),
            last_timestamp=datetime(2026, 10, 1, 1, minute, tzinfo=UTC),
        )
        for reason, identity, kind, name, minute in (
            ("Pulled", "pod-payment-1", "Pod", "payment-service-7d9c-1", 15),
            ("NewReplicaSetAvailable", "deployment-payment", "Deployment", "payment-service", 25),
        )
    )
    return KubernetesSnapshot(deployments=snapshot.deployments, pods=snapshot.pods, events=events)
