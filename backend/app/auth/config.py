"""单用户配置；没有默认账户、密码或签名密钥。"""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.auth.passwords import validate_password_hash


class AuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    username: str = Field(min_length=1, max_length=200)
    password_hash: SecretStr
    session_secret: SecretStr
    public_origin: str
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400, strict=True)
    max_login_failures: int = Field(default=5, ge=1, le=10, strict=True)
    login_lock_seconds: int = Field(default=60, ge=10, le=900, strict=True)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("username 不能包含首尾空白或控制字符")
        return value

    @field_validator("password_hash")
    @classmethod
    def valid_hash(cls, value: SecretStr) -> SecretStr:
        validate_password_hash(value.get_secret_value())
        return value

    @field_validator("session_secret")
    @classmethod
    def valid_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < 32 or any(char.isspace() for char in secret):
            raise ValueError("session_secret 必须至少 32 字符且不含空白，建议随机生成")
        return value

    @field_validator("public_origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.port != 0
                and url.username is None
                and url.password is None
                and url.path in {"", "/"}
                and not url.query
                and not url.fragment
                and "?" not in value
                and "#" not in value
                and not any(char.isspace() or ord(char) < 32 for char in value)
            )
            if url.scheme == "http":
                valid = valid and url.hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("public_origin 必须是 HTTPS Origin；本机回环可使用 HTTP")
        assert url.hostname is not None
        try:
            host = (
                f"[{url.hostname}]" if ":" in url.hostname else url.hostname.encode("idna").decode()
            )
        except UnicodeError:
            raise ValueError("public_origin 主机名无效") from None
        default_port = 443 if url.scheme == "https" else 80
        port = f":{url.port}" if url.port not in {None, default_port} else ""
        return f"{url.scheme}://{host}{port}"

    @property
    def secure_cookie(self) -> bool:
        return self.public_origin.startswith("https://")

    @property
    def cookie_name(self) -> str:
        return "__Host-ops_session" if self.secure_cookie else "ops_session"
