"""带随机盐的 PBKDF2；不保存或输出明文密码。"""

import hashlib
import hmac
import re
import secrets

ITERATIONS = 600_000
PASSWORD_HASH = re.compile(r"pbkdf2_sha256\$600000\$([0-9a-f]{32})\$([0-9a-f]{64})")


def validate_password_hash(value: str) -> str:
    if PASSWORD_HASH.fullmatch(value) is None:
        raise ValueError("password_hash 必须是本项目生成的 PBKDF2-SHA256 密码摘要")
    return value


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 256:
        raise ValueError("密码长度必须为 12–256 个字符")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    validate_password_hash(encoded)
    _, _, salt, expected = encoded.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return hmac.compare_digest(actual.hex(), expected)
