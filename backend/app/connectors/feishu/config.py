"""飞书自建应用的通知配置与独立凭证；只由环境变量注入。"""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class FeishuConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", hide_input_in_errors=True
    )

    # 绑定本人 open_id，发送接口不接受任意收件人或群聊。
    recipient_open_id: str = Field(pattern=r"^ou_[A-Za-z0-9_-]{1,128}$")
    timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)


class FeishuNotificationCredentials(BaseModel):
    """通知身份不能作为 AI Reader 或 AI Executor 使用。"""

    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", hide_input_in_errors=True
    )

    app_id: str = Field(pattern=r"^cli_[A-Za-z0-9_-]{1,128}$")
    app_secret: SecretStr

    @field_validator("app_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret or any(char.isspace() or ord(char) < 32 for char in secret):
            raise ValueError("飞书通知 app_secret 不能为空或含空白、控制字符")
        return value
