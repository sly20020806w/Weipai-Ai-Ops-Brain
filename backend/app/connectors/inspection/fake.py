"""Fake 所有检查有明确测量和来源；可注入异常/缺数据/过期场景。"""

from datetime import UTC, datetime

from app.connectors.inspection.client import InspectionConnector
from app.connectors.inspection.models import InspectionFact, InspectionFacts, InspectionQuery
from app.tasks.inspection.catalog import CHECKS


def sample_facts(service: str, *, abnormal: bool = True) -> InspectionFacts:
    now = datetime.now(UTC)
    facts = []
    for check in CHECKS:
        value: bool | float
        if check.operator in {"true", "false"}:
            value = check.operator == "true"
        else:
            value = check.threshold + 10 if check.operator == "min" else 0.1
        if abnormal:
            value = {
                "pdb_present": False,
                "hpa_present": False,
                "certificate_days": 6.0,
                "ecs_idle": True,
            }.get(check.id, value)
        resource = "ecs-unused" if check.id == "ecs_idle" else service
        facts.append(
            InspectionFact(
                check_id=check.id,
                resource=resource,
                value=value,
                source_reference=f"fake://inspection/{service}/{check.id}/{resource}",
                observed_at=now,
            )
        )
    return InspectionFacts(service_name=service, complete=True, facts=tuple(facts))


class FakeInspectionConnector(InspectionConnector):
    def __init__(self, snapshot: InspectionFacts | None = None) -> None:
        super().__init__()
        self.snapshot = snapshot.model_copy(deep=True) if snapshot else None
        self.closed = False
        self.calls = 0

    async def query(self, query: InspectionQuery) -> InspectionFacts:
        query = InspectionQuery.model_validate(query)
        if self.closed:
            raise RuntimeError("Fake 巡检 Connector 已关闭")
        self.calls += 1
        result = self.snapshot or sample_facts(query.service_name)
        if result.service_name != query.service_name:
            raise ValueError("Fake 服务范围错误")
        return InspectionFacts.model_validate(result).model_copy(deep=True)

    async def aclose(self) -> None:
        self.closed = True
