"""纯证据规则：未知不当作健康，回收只能恢复本次确实增加的副本。"""

import math
from datetime import datetime
from uuid import UUID

from app.connectors.war_room.facts import WarRoomFacts
from app.executor.models import ExecutionTarget
from app.tasks.inspection.engine import evaluate
from app.tasks.inspection.models import CheckResult, InspectionConfig
from app.tasks.war_room.models import WarRoomConfig, WarRoomSubmission


def assess_facts(
    value: WarRoomSubmission,
    config: WarRoomConfig,
    inspection: InspectionConfig,
    facts: WarRoomFacts,
    target: ExecutionTarget,
    baseline: ExecutionTarget,
    evidence_id: UUID,
    now: datetime,
    *,
    purpose: str,
    owned: ExecutionTarget | None,
) -> tuple[tuple[CheckResult, ...], int, bool, bool, bool, bool, bool]:
    if facts.query.service_name != value.service_name or target.service_name != value.service_name:
        raise ValueError("保障事实与资源服务不匹配")
    checks = evaluate(
        value.service_name, "governance", inspection, facts.inspection, evidence_id, now
    )
    fresh = 0 <= (now - facts.observed_at).total_seconds() <= inspection.max_age_seconds
    capacity_known = (
        fresh
        and facts.inspection.complete
        and facts.per_replica_rps is not None
        and facts.current_rps is not None
        and facts.ready_replicas is not None
        and facts.rollback_ready is not None
    )
    required = baseline.replicas
    if capacity_known:
        assert facts.per_replica_rps is not None and facts.current_rps is not None
        demand = value.projected_rps if purpose == "prepare" else facts.current_rps
        required = max(1, math.ceil(demand * config.capacity_margin / facts.per_replica_rps))
    complete = capacity_known and all(c.outcome != "unknown" for c in checks)
    ownership = target == (owned or baseline)
    health = all(c.outcome == "healthy" for c in checks)
    ready = facts.ready_replicas is not None and facts.ready_replicas >= target.replicas
    safe = bool(
        complete
        and health
        and ready
        and facts.rollback_ready
        and not target.paused
        and target.traffic_percent == 100
        and (
            purpose != "cleanup"
            or (now >= value.end and ownership and required <= baseline.replicas)
        )
    )
    anomaly = bool(complete and (not health or not ready or required > target.replicas))
    return checks, required, capacity_known, safe, complete, anomaly, ownership
