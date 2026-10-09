"""连接地址仅从环境变量读取；迁移使用 SQLAlchemy 异步引擎。"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection

from alembic import context
from app.auth import models as auth_models  # noqa: F401  # 注册登录会话元数据。
from app.config import Settings
from app.db.base import Base
from app.db.migrations import render_database_type
from app.db.session import Database
from app.graph import models as graph_models  # noqa: F401  # 注册 Context Graph 元数据。
from app.graph.changes import models as change_models  # noqa: F401  # 注册变更事件元数据。
from app.knowledge import human_drafts as human_draft_models  # noqa: F401
from app.knowledge import models as knowledge_models  # noqa: F401  # 注册知识条目元数据。
from app.ledger import models as ledger_models  # noqa: F401  # 注册证据与审计元数据。
from app.runbooks import models as runbook_models  # noqa: F401  # 注册 Runbook 元数据。
from app.tasks import models as task_models  # noqa: F401  # 注册业务模型元数据。
from app.tasks.inspection import models as inspection_models  # noqa: F401
from app.triggers import models as event_models  # noqa: F401  # 注册事件元数据。
from app.triggers.detection import models as detection_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 业务模型需显式导入，供 autogenerate 收集元数据。
target_metadata = Base.metadata
database_url = Settings().require_database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_database_type,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_item=render_database_type,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    database = Database(database_url)
    try:
        async with database.engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await database.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
