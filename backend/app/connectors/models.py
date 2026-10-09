"""Connector 配置与隔离的凭证类型；不签发执行权限。"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class ConnectorMode(StrEnum):
    FAKE = "fake"
    REAL = "real"


class _Credentials(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", hide_input_in_errors=True
    )

    connector: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    token: SecretStr

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Connector 凭证不能为空")
        return value


class ReaderCredentials(_Credentials):
    """仅交给只读 Connector 的 AI Reader 身份。"""


class ExecutorCredentials(_Credentials):
    """独立的 AI Executor 类型；动作授权和短时签发在 Step 32 实现。"""


def validate_credential_separation(
    reader: ReaderCredentials, executor: ExecutorCredentials
) -> None:
    if not isinstance(reader, ReaderCredentials) or not isinstance(executor, ExecutorCredentials):
        raise TypeError("读写凭证类型必须分别为 ReaderCredentials 和 ExecutorCredentials")
    if reader.connector != executor.connector:
        raise ValueError("读写凭证必须属于同一个 Connector")
    if reader.token.get_secret_value() == executor.token.get_secret_value():
        raise ValueError("AI Reader 与 AI Executor 不能复用同一凭证")
