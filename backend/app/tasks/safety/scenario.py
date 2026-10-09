"""六类熔断的 Fake 已采集事实，仅用于本机隔离演示与离线验收。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.investigation import AgentConclusion, EvidenceClaim
from app.agent.reviewer.models import AlternativeCause, ReviewCheck, ReviewDecision, ReviewReport
from app.connectors.observability.models import MetricPoint, MetricSeries, Span, TraceRecord
from app.ledger.models import AuditEventType, AuditRecord, Evidence
from app.runbooks.scenario import payment_runbook
from app.runbooks.schemas import RunbookView
from app.tasks.safety.models import AbortReason, AuditFact, EvidenceFact
from app.tools.observability import MetricsInput, MetricsOutput, TracesOutput, WindowInput
from app.tools.registry import json_object

NOW = datetime(2026, 10, 1, 2, tzinfo=UTC)


def fact(
    source: str, parameters: dict[str, object], result: dict[str, object], index: int = 0
) -> EvidenceFact:
    return EvidenceFact(
        uuid4(),
        source,
        json_object(parameters),
        json_object(result),
        NOW + timedelta(seconds=index),
    )


def audit(
    source: str, outcome: str, evidence: EvidenceFact | None = None, index: int = 0
) -> AuditFact:
    return AuditFact(
        uuid4(),
        source,
        outcome,
        NOW + timedelta(seconds=index),
        evidence.id if evidence else None,
        {"mode": "live"},
    )


def metric_fact(
    values: tuple[float, ...], *, metric: str = "http_5xx_ratio", offset: int = 0
) -> EvidenceFact:
    start = NOW + timedelta(minutes=offset)
    query = MetricsInput(
        service_name="payment-service",
        metric_name=metric,
        start=start,
        end=start + timedelta(minutes=len(values)),
    )
    output = MetricsOutput(
        service_name=query.service_name,
        start=query.start,
        end=query.end,
        series=(
            MetricSeries(
                service_name=query.service_name,
                metric_name=metric,
                labels={"instance": "payment-1"},
                points=tuple(
                    MetricPoint(timestamp=start + timedelta(minutes=i), value=v)
                    for i, v in enumerate(values)
                ),
            ),
        ),
    )
    return fact(
        "query_metrics", query.model_dump(mode="json"), output.model_dump(mode="json"), offset
    )


def trace_fact(services: tuple[str, ...], *, offset: int = 0) -> EvidenceFact:
    start = NOW + timedelta(minutes=offset)
    query = WindowInput(
        service_name="payment-service", start=start, end=start + timedelta(minutes=3)
    )
    trace = TraceRecord(
        trace_id=f"fake-trace-{offset}",
        service_name=query.service_name,
        timestamp=start,
        duration_ms=800,
        source_ref=f"arms://fake/{offset}",
        spans=tuple(
            Span(
                trace_id=f"fake-trace-{offset}",
                span_id=f"span-{i}",
                service_name=s,
                timestamp=start,
                duration_ms=800,
                operation="Fake 故障调用",
                result_code="500",
            )
            for i, s in enumerate(services)
        ),
    )
    output = TracesOutput(
        service_name=query.service_name,
        start=query.start,
        end=query.end,
        traces=(trace,),
        topology=(),
    )
    return fact(
        "query_traces", query.model_dump(mode="json"), output.model_dump(mode="json"), offset
    )


def case_facts(reason: AbortReason) -> tuple[list[EvidenceFact], list[AuditFact]]:
    if reason is AbortReason.EXECUTION_FAILURES:
        return [], [audit("execute_action", "failed", index=i) for i in range(3)]
    if reason is AbortReason.METRICS_WORSENING:
        item = metric_fact((0.02, 0.05, 0.09))
        return [item], [audit(item.source, "succeeded", item)]
    if reason is AbortReason.IMPACT_EXPANDING:
        items = [
            trace_fact(("payment-service",)),
            trace_fact(("payment-service", "checkout-service"), offset=3),
        ]
        return items, [audit(e.source, "succeeded", e, index=i) for i, e in enumerate(items)]
    if reason is AbortReason.EVIDENCE_CONFLICT:
        original = metric_fact((0.05, 0.05, 0.05))
        counter = trace_fact(("payment-service",))
        claim = EvidenceClaim(statement="主 Agent 将故障归因于连接池", evidence_ids=(original.id,))
        conclusion = AgentConclusion(root_cause=claim, findings=(claim,), confidence=0.7)
        accepted = fact(
            "agent.conclusion", {"phase_version": 4}, conclusion.model_dump(mode="json"), 1
        )
        report = ReviewReport(
            checks=tuple(
                ReviewCheck(
                    alternative=alternative,
                    outcome="contradicts"
                    if alternative is AlternativeCause.NETWORK
                    else "not_supported",
                    statement="Fake 独立反证",
                    evidence_ids=(counter.id,),
                )
                for alternative in AlternativeCause
            )
        )
        decision = ReviewDecision(
            conclusion_evidence_id=accepted.id,
            original_confidence=0.7,
            conclusion=conclusion,
            report=report,
            observed_ids=(counter.id,),
            steps=5,
        )
        verdict = fact(
            "reviewer.verdict", {"phase_version": 4}, decision.model_dump(mode="json"), 2
        )
        return [original, counter, accepted, verdict], [
            audit(e.source, "succeeded", e) for e in (original, counter)
        ]
    if reason is AbortReason.RUNBOOK_FAILURES:
        draft = payment_runbook()
        view = RunbookView(
            **draft.model_dump(),
            id=uuid4(),
            created_at=NOW,
            updated_at=NOW,
            embedding_model="fake",
            embedding_dimensions=3,
        )
        match = fact(
            "runbook.match",
            {"phase_version": 2},
            {"runbook_json": view.model_dump_json(), "blocked": False},
        )
        attempts = [audit("query_logs", "failed", index=i) for i in (1, 2)]
        results = [
            fact(
                "agent.observe",
                {"phase_version": phase},
                {"status": "failed", "audit_id": str(a.id)},
                index=i,
            )
            for i, (phase, a) in enumerate(zip((3, 5), attempts, strict=True), start=1)
        ]
        return [match, *results], attempts
    items = [
        fact(
            "execution.intent",
            {"plan_evidence_id": "fake-plan", "action_id": f"action-{i}"},
            {"execution_id": str(uuid4())},
            i,
        )
        for i in range(4)
    ]
    return items, []


async def seed_case(session: AsyncSession, task_id: UUID, reason: AbortReason) -> None:
    evidence, audits = case_facts(reason)
    # 显式 Fake 已采集快照；保留引用 ID，不访问生产或绕过只追加约束修改记录。
    for item in evidence:
        session.add(
            Evidence(
                id=item.id,
                task_id=task_id,
                source_tool=item.source,
                parameters=item.parameters,
                result_snapshot=item.result,
                collected_at=item.collected_at,
            )
        )
    await session.flush()
    for entry in audits:
        session.add(
            AuditRecord(
                id=entry.id,
                task_id=task_id,
                event_type=AuditEventType.TOOL_CALL,
                actor="fake-safety-scenario",
                operation=entry.operation,
                outcome=entry.outcome,
                details=entry.details,
                evidence_id=entry.evidence_id,
                occurred_at=entry.occurred_at,
            )
        )
    await session.flush()
