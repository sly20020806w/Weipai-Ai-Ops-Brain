"""建立数据库迁移基线；业务表由后续步骤添加。"""

revision: str = "0001_database_foundation"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
