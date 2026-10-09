"""按源身份去重；不同历史事实不能被重试静默覆盖。事务由调用方管理。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.changes.models import DeploymentQuery
from app.graph.changes.models import ChangeEvent
from app.graph.changes.schemas import ChangeSnapshot, ChangeView, TimelineResult


class ChangeConflict(ValueError):
    pass


def view(event: ChangeEvent) -> ChangeView:
    return ChangeView.model_validate(
        {
            "id": event.id,
            "service_name": event.service_name,
            "source": event.source,
            "kind": event.kind,
            "source_ref": event.source_ref,
            "occurred_at": event.occurred_at,
            "revision": event.revision,
            "collected_at": event.created_at,
        }
    )


class TimelineService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def persist(self, snapshot: ChangeSnapshot) -> TimelineResult:
        snapshot = ChangeSnapshot.model_validate(snapshot)
        inserted = 0
        # 一致排序降低并发采集的唯一键死锁风险。
        for fact in sorted(snapshot.events, key=lambda e: e.identity):
            statement = (
                insert(ChangeEvent)
                .values(**fact.model_dump())
                .on_conflict_do_nothing(
                    index_elements=["service_name", "source", "kind", "source_ref"]
                )
                .returning(ChangeEvent.id)
            )
            if await self.session.scalar(statement) is not None:
                inserted += 1
            else:
                existing = await self.session.scalar(
                    select(ChangeEvent).where(
                        ChangeEvent.service_name == fact.service_name,
                        ChangeEvent.source == fact.source,
                        ChangeEvent.kind == fact.kind,
                        ChangeEvent.source_ref == fact.source_ref,
                    )
                )
                if existing is None or (existing.occurred_at, existing.revision) != (
                    fact.occurred_at,
                    fact.revision,
                ):
                    raise ChangeConflict("同一源变更引用存在证据冲突")
        return TimelineResult(
            snapshot.service_name,
            snapshot.start.isoformat(),
            snapshot.end.isoformat(),
            len(snapshot.events),
            inserted,
            snapshot.missing_bindings,
        )

    async def recent(
        self, service_name: str, start: datetime, end: datetime
    ) -> tuple[ChangeView, ...]:
        query = DeploymentQuery(service_name=service_name, start=start, end=end)
        events = await self.session.scalars(
            select(ChangeEvent)
            .where(
                ChangeEvent.service_name == query.service_name,
                ChangeEvent.occurred_at >= query.start,
                ChangeEvent.occurred_at < query.end,
            )
            .order_by(
                ChangeEvent.occurred_at,
                ChangeEvent.source,
                ChangeEvent.kind,
                ChangeEvent.source_ref,
                ChangeEvent.id,
            )
        )
        return tuple(view(event) for event in events)
