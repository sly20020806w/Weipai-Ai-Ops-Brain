"""外部系统适配器的公共生命周期；不提供通用请求或写操作。"""

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self

from app.connectors.models import ExecutorCredentials, ReaderCredentials


class Connector(ABC):
    @abstractmethod
    async def aclose(self) -> None:
        """释放客户端资源，具体业务接口由后续 Connector 定义。"""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


class ReadOnlyConnector(Connector):
    def __init__(self, credentials: ReaderCredentials | None = None) -> None:
        if credentials is not None and not isinstance(credentials, ReaderCredentials):
            raise TypeError("只读 Connector 只接受 ReaderCredentials")
        self._reader_credentials = (
            ReaderCredentials.model_validate(credentials) if credentials is not None else None
        )

    @property
    def reader_credentials(self) -> ReaderCredentials | None:
        return self._reader_credentials


class WriteConnector(Connector):
    """写适配器的独立基类；当前阶段没有写方法和创建入口。"""

    def __init__(self, credentials: ExecutorCredentials) -> None:
        if not isinstance(credentials, ExecutorCredentials):
            raise TypeError("写 Connector 只接受 ExecutorCredentials")
        self._executor_credentials = ExecutorCredentials.model_validate(credentials)
