"""Fake 与真实实现共用的唯一只读业务接口。"""

from abc import abstractmethod

from app.connectors.base import ReadOnlyConnector
from app.connectors.cloud.models import CloudQuery, CloudResources, CloudTopic


class CloudError(RuntimeError):
    pass


class CloudNotFound(CloudError):
    pass


class CloudResponseError(CloudError):
    pass


class CloudTimeout(CloudError):
    pass


class CloudHTTPError(CloudError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"阿里云只读请求失败（HTTP {status_code}）")


class CloudConnector(ReadOnlyConnector):
    @abstractmethod
    async def list_topics(self, service_name: str) -> tuple[CloudTopic, ...]: ...

    @abstractmethod
    async def get_cloud_resources(self, query: CloudQuery) -> CloudResources: ...
