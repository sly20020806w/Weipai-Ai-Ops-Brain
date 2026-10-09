"""四种变更来源的共享只读接口。"""

from abc import abstractmethod

from app.connectors.base import ReadOnlyConnector
from app.connectors.changes.models import (
    BuildRecord,
    ChangeRecord,
    CodeComparison,
    ConfigComparison,
    DeploymentQuery,
    DeploymentRecord,
    Repository,
    VersionQuery,
)


class ChangesError(RuntimeError):
    pass


class ChangesNotFound(ChangesError):
    pass


class ChangesResponseError(ChangesError):
    pass


class ChangesTimeout(ChangesError):
    pass


class ChangesHTTPError(ChangesError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"变更链路读取失败（HTTP {status_code}）")


class GitConnector(ReadOnlyConnector):
    @abstractmethod
    async def list_changes(self, query: DeploymentQuery) -> tuple[ChangeRecord, ...]: ...

    @abstractmethod
    async def get_repository(self, service_name: str) -> Repository: ...

    @abstractmethod
    async def compare_versions(self, query: VersionQuery) -> CodeComparison: ...


class CIConnector(ReadOnlyConnector):
    @abstractmethod
    async def list_builds(self, query: DeploymentQuery) -> tuple[BuildRecord, ...]: ...


class ArgoCDConnector(ReadOnlyConnector):
    @abstractmethod
    async def list_deployments(self, query: DeploymentQuery) -> tuple[DeploymentRecord, ...]: ...


class ConfigCenterConnector(ReadOnlyConnector):
    @abstractmethod
    async def list_changes(self, query: DeploymentQuery) -> tuple[ChangeRecord, ...]: ...

    @abstractmethod
    async def compare_versions(self, query: VersionQuery) -> ConfigComparison: ...
