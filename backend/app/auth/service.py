"""登录限速、可撤销会话和身份验证；不涉及运维系统或任务状态迁移。"""

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from app.auth.config import AuthConfig
from app.auth.identity import Principal
from app.auth.models import AuthSession, LoginGuard
from app.auth.passwords import verify_password
from app.db.base import utc_now
from app.db.session import Database

GUARD_ID = UUID("00000000-0000-0000-0000-000000000001")
TOKEN = re.compile(r"([0-9a-f]{32})\.([0-9a-f]{64})")


class LoginRejected(ValueError):
    def __init__(self, *, locked: bool = False) -> None:
        super().__init__("登录暂时锁定" if locked else "用户名或密码错误")
        self.locked = locked


@dataclass(frozen=True)
class IssuedSession:
    principal: Principal
    cookie: str = field(repr=False)


class AuthService:
    def __init__(self, database: Database, config: AuthConfig) -> None:
        self.database = database
        self.config = config

    def _mac(self, purpose: str, session_id: UUID) -> str:
        # 密码、账户或密钥轮换立即使旧 Cookie 和 CSRF token 失效。
        payload = "\0".join(
            (
                purpose,
                session_id.hex,
                self.config.username,
                self.config.password_hash.get_secret_value(),
                self.config.public_origin,
            )
        )
        return hmac.new(
            self.config.session_secret.get_secret_value().encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    def cookie_for(self, session_id: UUID) -> str:
        return f"{session_id.hex}.{self._mac('session', session_id)}"

    def _principal(self, record: AuthSession) -> Principal:
        return Principal(record.actor, record.id, record.expires_at, self._mac("csrf", record.id))

    async def login(self, username: str, password: str) -> IssuedSession:
        rejected: LoginRejected | None = None
        issued: IssuedSession | None = None
        async with self.database.session() as session, session.begin():
            now = utc_now()
            await session.execute(
                insert(LoginGuard)
                .values(id=GUARD_ID, failures=0, window_started_at=now)
                .on_conflict_do_nothing(index_elements=[LoginGuard.id])
            )
            guard = await session.scalar(
                select(LoginGuard).where(LoginGuard.id == GUARD_ID).with_for_update()
            )
            assert guard is not None
            now = utc_now()
            if guard.locked_until is not None and now < guard.locked_until:
                rejected = LoginRejected(locked=True)
            else:
                if (
                    now - guard.window_started_at
                ).total_seconds() >= self.config.login_lock_seconds:
                    guard.failures = 0
                    guard.window_started_at = now
                guard.locked_until = None
                correct_password = await run_in_threadpool(
                    verify_password, password, self.config.password_hash.get_secret_value()
                )
                correct_username = hmac.compare_digest(
                    username.encode(), self.config.username.encode()
                )
                if correct_password and correct_username:
                    guard.failures = 0
                    record = AuthSession(
                        id=uuid4(),
                        actor=self.config.username,
                        created_at=now,
                        expires_at=now + timedelta(seconds=self.config.session_ttl_seconds),
                    )
                    session.add(record)
                    await session.flush()
                    issued = IssuedSession(self._principal(record), self.cookie_for(record.id))
                else:
                    guard.failures += 1
                    if guard.failures >= self.config.max_login_failures:
                        guard.locked_until = now + timedelta(seconds=self.config.login_lock_seconds)
                    rejected = LoginRejected()
        if rejected is not None:
            raise rejected
        assert issued is not None
        return issued

    async def authenticate(self, cookie: str) -> Principal | None:
        match = TOKEN.fullmatch(cookie)
        if match is None:
            return None
        session_id = UUID(hex=match[1])
        if not hmac.compare_digest(match[2], self._mac("session", session_id)):
            return None
        async with self.database.session() as session:
            record = await session.get(AuthSession, session_id)
            now = utc_now()
            if (
                record is None
                or record.actor != self.config.username
                or record.revoked_at is not None
                or not record.created_at <= now < record.expires_at
            ):
                return None
            return self._principal(record)

    async def logout(self, principal: Principal) -> None:
        async with self.database.session() as session, session.begin():
            await session.execute(
                update(AuthSession)
                .where(AuthSession.id == principal.session_id, AuthSession.revoked_at.is_(None))
                .values(revoked_at=utc_now())
            )
