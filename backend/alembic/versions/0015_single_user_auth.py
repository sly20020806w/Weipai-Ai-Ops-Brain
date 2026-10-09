"""持久化可撤销会话及单用户登录限速；密钥不入库。"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_single_user_auth"
down_revision: str | Sequence[str] | None = "0014_inspection_risks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(trim(actor)) > 0", name="actor_not_blank"),
        sa.CheckConstraint("expires_at > created_at", name="expiry_valid"),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="revocation_valid"
        ),
    )
    op.create_table(
        "auth_login_guard",
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("failures >= 0", name="failures_nonnegative"),
    )


def downgrade() -> None:
    # 降级撤销全部登录；运维证据和审计保留在原 Ledger，不受影响。
    op.drop_table("auth_login_guard")
    op.drop_table("auth_sessions")
