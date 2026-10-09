"""将运行时 UTC 类型渲染为迁移中的原生 PostgreSQL 时间列。"""

from typing import Literal

from alembic.autogenerate.api import AutogenContext
from pgvector.sqlalchemy import VECTOR  # type: ignore[import-untyped]  # 官方适配器未带类型声明。

from app.db.base import UTCDateTime


def render_database_type(
    kind: str, item: object, autogen_context: AutogenContext
) -> str | Literal[False]:
    if kind == "type" and isinstance(item, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    if kind == "type" and isinstance(item, VECTOR):
        autogen_context.imports.add("from pgvector.sqlalchemy import VECTOR")
        return "VECTOR()" if item.dim is None else f"VECTOR({item.dim})"
    return False
