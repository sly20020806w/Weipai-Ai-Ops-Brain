"""仅供本机 Fake 验收的账户；真实 API 没有默认账户。"""

from app.auth.config import AuthConfig
from app.auth.passwords import hash_password

PASSWORD = "local-test-password-43"
PASSWORD_HASH = hash_password(PASSWORD)


def auth_config(**changes: object) -> AuthConfig:
    return AuthConfig.model_validate(
        {
            "username": "local-owner",
            "password_hash": PASSWORD_HASH,
            "session_secret": "offline-session-secret-" + "a" * 32,
            "public_origin": "http://127.0.0.1",
            **changes,
        }
    )
