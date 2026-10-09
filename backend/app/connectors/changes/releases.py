"""发布系统只读接口及 Fake；事实仍由源平台提供，写通道独立。"""

from abc import ABC, abstractmethod

from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector, fake_binding
from app.db.base import utc_now
from app.tasks.releases.models import (
    ReleaseManifest,
    ReleaseObservation,
    ReleaseQuery,
    ReleaseWindow,
)


class ReleaseReader(ABC):
    @abstractmethod
    async def manifest(self, query: ReleaseQuery) -> ReleaseManifest: ...

    @abstractmethod
    async def observe(self, query: ReleaseWindow) -> ReleaseObservation: ...


class FakeReleaseState:
    def __init__(self, *, scenario: str = "normal") -> None:
        if scenario not in {
            "normal",
            "anomaly",
            "high_sql",
            "missing",
            "deteriorating",
            "unrecovered",
        }:
            raise ValueError("未知发布 Fake 场景")
        self.scenario = scenario
        self.writer = FakeKubernetesWriteConnector()
        target = self.writer.targets["payment-service"]
        self.writer.targets["payment-service"] = target.model_copy(
            update={"image": fake_binding().images["v2.3.6"]}
        )
        self.requests: dict[str, ReleaseManifest] = {}
        self.read_count = 0

    def add(self, release_id: str) -> None:
        self.requests[release_id] = ReleaseManifest(
            release_id=release_id,
            service_name="payment-service",
            from_version="v2.3.6",
            to_version="v2.3.7",
            sql=("DROP TABLE payments;",) if self.scenario == "high_sql" else (),
            resource_ready=True,
            monitoring_ready=True,
            rollback_ready=True,
            reference="fake://releases/" + release_id,
        )


class FakeReleaseReader(ReleaseReader):
    def __init__(self, state: FakeReleaseState) -> None:
        self.state = state

    async def manifest(self, query: ReleaseQuery) -> ReleaseManifest:
        self.state.read_count += 1
        return self.state.requests[query.release_id].model_copy(deep=True)

    async def observe(self, query: ReleaseWindow) -> ReleaseObservation:
        if query.end > utc_now():
            raise ValueError("不得查询未来发布观测")
        self.state.read_count += 1
        request = self.state.requests[query.release_id]
        target = self.state.writer.targets[request.service_name].model_copy(deep=True)
        bad = target.image.endswith(":v2.3.7") and self.state.scenario in {
            "anomaly",
            "deteriorating",
            "unrecovered",
        }
        if self.state.scenario == "unrecovered" and self.state.writer.execution_count:
            bad = True
        missing = self.state.scenario == "missing" and self.state.writer.execution_count > 0
        return ReleaseObservation(
            **query.model_dump(),
            service_name=request.service_name,
            target=target,
            deployment_ready=True,
            pods_ready=True,
            http_5xx=()
            if missing
            else (0.03, 0.06, 0.09)
            if bad and self.state.scenario == "deteriorating"
            else (0.05,) * 3
            if bad
            else (0.001,) * 3,
            p99_ms=(800.0,) * 3 if bad else (120.0,) * 3,
            success_ratio=(0.95,) * 3 if bad else (0.999,) * 3,
            logs_healthy=not bad,
            traces_healthy=not bad,
            resources_healthy=True,
        )
