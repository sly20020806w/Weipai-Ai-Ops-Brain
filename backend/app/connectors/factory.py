"""按宿主配置选择已注册的只读适配器；不导入或自动发现外部 SDK。"""

import re
from collections.abc import Callable

from app.config import Settings
from app.connectors.base import ReadOnlyConnector, WriteConnector
from app.connectors.models import ConnectorMode, ReaderCredentials


class ConnectorFactory[Reader: ReadOnlyConnector]:
    def __init__(
        self,
        name: str,
        *,
        fake: Callable[[], Reader],
        real: Callable[[ReaderCredentials], Reader],
    ) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None:
            raise ValueError("Connector 名称必须是最多 64 字符的英文标识符")
        self._name = name
        self._fake = fake
        self._real = real

    def create(self, settings: Settings) -> Reader:
        # 重新验证宿主配置，防止赋值或 model_copy 绕过 local/test 门禁。
        config = Settings.model_validate(
            {
                field.validation_alias
                if isinstance(field.validation_alias, str)
                else name: getattr(settings, name)
                for name, field in Settings.model_fields.items()
            }
        )
        if config.connector_mode is ConnectorMode.FAKE:
            reader = self._fake()
            if (
                not isinstance(reader, ReadOnlyConnector)
                or isinstance(reader, WriteConnector)
                or reader.reader_credentials is not None
            ):
                raise TypeError("Fake 工厂必须返回不携带真实凭证的只读 Connector")
            return reader
        token = config.connector_reader_tokens.get(self._name)
        if token is None:
            raise ValueError(f"请在 CONNECTOR_READER_TOKENS 中设置 {self._name} 的只读凭证")
        credentials = ReaderCredentials(connector=self._name, token=token)
        reader = self._real(credentials)
        if (
            not isinstance(reader, ReadOnlyConnector)
            or isinstance(reader, WriteConnector)
            or reader.reader_credentials != credentials
        ):
            raise TypeError("真实工厂必须返回绑定该 Reader 凭证的只读 Connector")
        return reader
