"""HTTP 层数据库依赖；不包含业务逻辑。"""

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(503, "请通过环境变量设置 DATABASE_URL")
    async with database.session() as session:
        yield session
