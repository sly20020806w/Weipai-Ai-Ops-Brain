"""持久化会话仅存公开 ID、操作人和 UTC 期限，不保存可用的登录凭证。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("length(trim(actor)) > 0", name="actor_not_blank"),
        CheckConstraint("expires_at > created_at", name="expiry_valid"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at", name="revocation_valid"),
    )

    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class LoginGuard(Base):
    __tablename__ = "auth_login_guard"
    __table_args__ = (CheckConstraint("failures >= 0", name="failures_nonnegative"),)

    failures: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
