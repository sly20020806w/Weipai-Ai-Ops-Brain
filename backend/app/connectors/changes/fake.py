"""可注入、可重复的离线快照；不存在的比较失败，不编造差异。"""

from datetime import UTC, datetime
from typing import Literal

from app.connectors.changes.base import (
    ArgoCDConnector,
    ChangesError,
    ChangesNotFound,
    CIConnector,
    ConfigCenterConnector,
    GitConnector,
)
from app.connectors.changes.models import (
    BuildRecord,
    ChangeRecord,
    CodeComparison,
    ConfigComparison,
    ConfigVersion,
    DeploymentQuery,
    DeploymentRecord,
    FileDiff,
    Repository,
    ServiceName,
    VersionQuery,
    config_diff,
)

SAMPLE_START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
SAMPLE_END = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
SAMPLE_PATCH = "@@ -1 +1 @@\n-db.pool.max_connections: 50\n+db.pool.max_connections: 500\n"


class FakeLifecycle:
    def __init__(self) -> None:
        self._closed = False

    def check_open(self) -> None:
        if self._closed:
            raise ChangesError("Fake 变更链路 Connector 已关闭")

    async def aclose(self) -> None:
        self._closed = True


class FakeGitConnector(FakeLifecycle, GitConnector):
    def __init__(
        self,
        comparisons: tuple[CodeComparison, ...] | None = None,
        *,
        provider: Literal["gitlab", "github"] = "gitlab",
        repositories: tuple[Repository, ...] | None = None,
        changes: tuple[ChangeRecord, ...] | None = None,
    ) -> None:
        FakeLifecycle.__init__(self)
        GitConnector.__init__(self)
        self._changes = tuple(
            ChangeRecord.model_validate(item).model_copy(deep=True)
            for item in (
                changes
                if changes is not None
                else tuple(
                    ChangeRecord(
                        id=identity,
                        service_name="payment-service",
                        source=provider,
                        kind="Merge" if kind == "Merge" else "Commit",
                        timestamp=datetime(2026, 10, 1, 1, minute, tzinfo=UTC),
                        source_ref=f"{provider}:weipai/payment-service:{identity}",
                        revision=identity,
                    )
                    for identity, kind, minute in (
                        ("sha237", "Commit", 0),
                        ("merge237", "Merge", 5),
                    )
                )
            )
        )
        default = CodeComparison(
            service_name="payment-service",
            from_version="v2.3.6",
            to_version="v2.3.7",
            source=provider,
            repository="weipai/payment-service",
            files=(
                FileDiff(
                    old_path="config/payment.yaml",
                    new_path="config/payment.yaml",
                    patch=SAMPLE_PATCH,
                    status="modified",
                ),
            ),
            comparison_kind="direct" if provider == "gitlab" else "merge_base",
            source_ref=f"{provider}:weipai/payment-service:v2.3.6..v2.3.7",
        )
        self._comparisons = tuple(
            CodeComparison.model_validate(item).model_copy(deep=True)
            for item in (comparisons if comparisons is not None else (default,))
        )

        self._repositories = (
            repositories
            if repositories is not None
            else (
                Repository(
                    service_name="payment-service",
                    source=provider,
                    repository_id="101",
                    name="weipai/payment-service",
                    source_ref=f"{provider}:101",
                ),
            )
        )

    async def get_repository(self, service_name: str) -> Repository:
        from pydantic import TypeAdapter

        self.check_open()
        TypeAdapter(ServiceName).validate_python(service_name, strict=True)
        for item in self._repositories:
            if item.service_name == service_name:
                return Repository.model_validate(item).model_copy(deep=True)
        raise ChangesNotFound("服务没有 Fake 仓库绑定")

    async def list_changes(self, query: DeploymentQuery) -> tuple[ChangeRecord, ...]:
        self.check_open()
        query = DeploymentQuery.model_validate(query)
        return tuple(
            item.model_copy(deep=True)
            for item in self._changes
            if item.service_name == query.service_name and query.contains(item.timestamp)
        )

    async def compare_versions(self, query: VersionQuery) -> CodeComparison:
        self.check_open()
        query = VersionQuery.model_validate(query)
        for item in self._comparisons:
            if (item.service_name, item.from_version, item.to_version) == (
                query.service_name,
                query.from_version,
                query.to_version,
            ):
                return item.model_copy(deep=True)
        raise ChangesNotFound("Fake 代码比较不存在")


def sample_builds(provider: Literal["jenkins", "gitlab_ci"]) -> tuple[BuildRecord, ...]:
    return tuple(
        BuildRecord(
            id=str(index),
            service_name="payment-service",
            source=provider,
            timestamp=datetime(2026, 10, 1, hour, minute, tzinfo=UTC),
            revision=revision,
            status="SUCCESS" if provider == "jenkins" else "success",
            source_ref=f"{provider}:payment:{index}",
        )
        for index, hour, minute, revision in (
            (36, 0, 10, "sha236"),
            (37, 1, 10, "sha237"),
            (38, 2, 0, "sha238"),
        )
    )


class FakeCIConnector(FakeLifecycle, CIConnector):
    def __init__(
        self,
        builds: tuple[BuildRecord, ...] | None = None,
        *,
        provider: Literal["jenkins", "gitlab_ci"] = "gitlab_ci",
    ) -> None:
        FakeLifecycle.__init__(self)
        CIConnector.__init__(self)
        self._builds = tuple(
            BuildRecord.model_validate(item).model_copy(deep=True)
            for item in (builds if builds is not None else sample_builds(provider))
        )

    async def list_builds(self, query: DeploymentQuery) -> tuple[BuildRecord, ...]:
        self.check_open()
        query = DeploymentQuery.model_validate(query)
        return tuple(
            item.model_copy(deep=True)
            for item in sorted(
                (
                    item
                    for item in self._builds
                    if item.service_name == query.service_name and query.contains(item.timestamp)
                ),
                key=lambda item: (item.timestamp, item.id),
                reverse=True,
            )
        )


class FakeArgoCDConnector(FakeLifecycle, ArgoCDConnector):
    def __init__(self, deployments: tuple[DeploymentRecord, ...] | None = None) -> None:
        FakeLifecycle.__init__(self)
        ArgoCDConnector.__init__(self)
        default = tuple(
            DeploymentRecord(
                id=str(index),
                service_name="payment-service",
                application="payment",
                timestamp=datetime(2026, 10, 1, hour, minute, tzinfo=UTC),
                revision=revision,
                source_ref=f"argocd:payment:{index}",
            )
            for index, hour, minute, revision in (
                (36, 0, 20, "sha236"),
                (37, 1, 20, "sha237"),
                (38, 2, 0, "sha238"),
            )
        )
        self._deployments = tuple(
            DeploymentRecord.model_validate(item).model_copy(deep=True)
            for item in (deployments if deployments is not None else default)
        )

    async def list_deployments(self, query: DeploymentQuery) -> tuple[DeploymentRecord, ...]:
        self.check_open()
        query = DeploymentQuery.model_validate(query)
        return tuple(
            item.model_copy(deep=True)
            for item in sorted(
                (
                    item
                    for item in self._deployments
                    if item.service_name == query.service_name and query.contains(item.timestamp)
                ),
                key=lambda item: (item.timestamp, item.id),
                reverse=True,
            )
        )


class FakeConfigCenterConnector(FakeLifecycle, ConfigCenterConnector):
    def __init__(
        self,
        versions: tuple[ConfigVersion, ...] | None = None,
        *,
        changes: tuple[ChangeRecord, ...] | None = None,
    ) -> None:
        FakeLifecycle.__init__(self)
        ConfigCenterConnector.__init__(self)
        self._changes = tuple(
            ChangeRecord.model_validate(item).model_copy(deep=True)
            for item in (
                changes
                if changes is not None
                else (
                    ChangeRecord(
                        id="config237",
                        service_name="payment-service",
                        source="config_center",
                        kind="Config",
                        timestamp=datetime(2026, 10, 1, 1, 30, tzinfo=UTC),
                        source_ref="config_center:payment:config237",
                        revision="v2.3.7",
                    ),
                )
            )
        )
        default = tuple(
            ConfigVersion(
                service_name="payment-service",
                version=version,
                values={"db.pool.max_connections": size, "db.pool.timeout_seconds": "5"},
            )
            for version, size in (("v2.3.6", "50"), ("v2.3.7", "500"))
        )
        self._versions = tuple(
            ConfigVersion.model_validate(item).model_copy(deep=True)
            for item in (versions if versions is not None else default)
        )

    async def compare_versions(self, query: VersionQuery) -> ConfigComparison:
        self.check_open()
        query = VersionQuery.model_validate(query)
        versions = {
            item.version: item for item in self._versions if item.service_name == query.service_name
        }
        if query.from_version not in versions or query.to_version not in versions:
            raise ChangesNotFound("Fake 配置版本不存在")
        return config_diff(versions[query.from_version], versions[query.to_version])

    async def list_changes(self, query: DeploymentQuery) -> tuple[ChangeRecord, ...]:
        self.check_open()
        query = DeploymentQuery.model_validate(query)
        return tuple(
            item.model_copy(deep=True)
            for item in self._changes
            if item.service_name == query.service_name and query.contains(item.timestamp)
        )
