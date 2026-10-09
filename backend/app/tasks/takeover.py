"""人工接管锁存；独立于 Workflow 进程存在，旧审批不能解除。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger.models import Evidence


async def takeover_record(session: AsyncSession, task_id: UUID) -> Evidence | None:
    record: Evidence | None = await session.scalar(
        select(Evidence).where(
            Evidence.task_id == task_id, Evidence.source_tool == "human.takeover"
        )
    )
    return record
