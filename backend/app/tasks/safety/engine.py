"""只计算同任务已提交事实，不查询系统、不迁移状态、不重试。"""

import json
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from app.agent.reviewer.models import ReviewDecision
from app.runbooks.schemas import RunbookView
from app.tasks.safety.models import (
    AbortReason,
    AuditFact,
    EvidenceFact,
    SafetyConfig,
    SafetyFinding,
)
from app.tools.models import DispatchStatus
from app.tools.observability import MetricsInput, MetricsOutput, TracesOutput, WindowInput
from app.verifier.models import VerificationReport


def evaluate(
    evidence: list[EvidenceFact],
    audits: list[AuditFact],
    config: SafetyConfig,
    *,
    pending_action: tuple[str, str] | None = None,
) -> tuple[SafetyFinding, ...]:
    config = SafetyConfig.model_validate(config)
    evidence = sorted(evidence, key=lambda e: (e.collected_at, str(e.id)))
    audits = sorted(audits, key=lambda a: (a.occurred_at, str(a.id)))
    findings: list[SafetyFinding] = []
    failures: list[UUID] = []
    for audit in audits:
        if audit.operation != "execute_action" or audit.details.get("mode") != "live":
            continue
        if audit.outcome == "succeeded":
            failures.clear()
        elif audit.outcome == "failed":
            failures.append(audit.id)
    if len(failures) >= config.max_consecutive_execution_failures:
        findings.append(
            SafetyFinding(reason=AbortReason.EXECUTION_FAILURES, audit_ids=tuple(failures))
        )

    by_id = {e.id: e for e in evidence}
    successful = {
        a.evidence_id
        for a in audits
        if a.outcome == DispatchStatus.SUCCEEDED.value
        and a.details.get("mode") == "live"
        and a.evidence_id in by_id
        and by_id[a.evidence_id].source == a.operation
    }
    metrics: dict[
        tuple[str, str, str], list[tuple[EvidenceFact, tuple[tuple[datetime, float], ...]]]
    ] = defaultdict(list)
    impacts: dict[str, list[tuple[EvidenceFact, datetime, datetime, frozenset[str]]]] = defaultdict(
        list
    )
    for fact in evidence:
        if fact.id not in successful:
            continue
        if fact.source == "query_release_observation":
            from app.tasks.releases.models import ReleaseObservation

            observed = ReleaseObservation.model_validate_json(json.dumps(fact.result))
            for release_values, threshold, delta, direction in (
                (observed.http_5xx, config.max_5xx_ratio, config.min_5xx_increase, 1),
                (observed.p99_ms, config.max_p99_ms, config.min_p99_increase_ms, 1),
                (observed.success_ratio, config.min_success_ratio, config.min_success_decrease, -1),
            ):
                directed = tuple(v * direction for v in release_values)
                if (
                    len(directed) >= config.min_metric_points
                    and directed[-1] > threshold * direction
                    and directed[-1] - directed[0] >= delta
                    and all(b >= a for a, b in zip(directed, directed[1:], strict=False))
                ):
                    findings.append(
                        SafetyFinding(reason=AbortReason.METRICS_WORSENING, evidence_ids=(fact.id,))
                    )
        if fact.source == "query_metrics":
            data = MetricsOutput.model_validate_json(json.dumps(fact.result))
            query = MetricsInput.model_validate_json(json.dumps(fact.parameters))
            if (data.service_name, data.start, data.end) != (
                query.service_name,
                query.start,
                query.end,
            ):
                continue
            for series in data.series:
                if (
                    series.service_name != data.service_name
                    or series.metric_name != query.metric_name
                ):
                    continue
                points = tuple(
                    sorted(
                        (p.timestamp, p.value) for p in series.points if data.contains(p.timestamp)
                    )
                )
                if len(points) < config.min_metric_points or len({p[0] for p in points}) != len(
                    points
                ):
                    continue
                if any(
                    (b[0] - a[0]).total_seconds() > config.max_sample_gap_seconds
                    for a, b in zip(points, points[1:], strict=False)
                ):
                    continue
                key = (
                    data.service_name,
                    series.metric_name,
                    json.dumps(series.labels, sort_keys=True),
                )
                metrics[key].append((fact, points))
        elif fact.source == "query_traces":
            trace_data = TracesOutput.model_validate_json(json.dumps(fact.result))
            query_window = WindowInput.model_validate_json(json.dumps(fact.parameters))
            if (trace_data.service_name, trace_data.start, trace_data.end) != (
                query_window.service_name,
                query_window.start,
                query_window.end,
            ):
                continue
            affected = frozenset(
                span.service_name
                for trace in trace_data.traces
                if trace.service_name == trace_data.service_name
                and trace_data.contains(trace.timestamp)
                for span in trace.spans
                if trace_data.contains(span.timestamp)
                and (
                    span.result_code.startswith("5")
                    or span.result_code in {"ERROR", "error", "timeout"}
                )
            )
            impacts[trace_data.service_name].append(
                (fact, trace_data.start, trace_data.end, affected)
            )

    rules = {
        "http_5xx_ratio": (1, config.max_5xx_ratio, config.min_5xx_increase),
        "http_p99_ms": (1, config.max_p99_ms, config.min_p99_increase_ms),
        "http_success_ratio": (-1, config.min_success_ratio, config.min_success_decrease),
    }
    for (_, metric, _), samples in metrics.items():
        if metric not in rules:
            continue
        direction, threshold, delta = rules[metric]
        samples.sort(key=lambda item: item[1][-1][0])
        for fact, points in samples:
            values = [p[1] * direction for p in points]
            if (
                values[-1] > threshold * direction
                and values[-1] - values[0] >= delta
                and all(b >= a for a, b in zip(values, values[1:], strict=False))
            ):
                findings.append(
                    SafetyFinding(reason=AbortReason.METRICS_WORSENING, evidence_ids=(fact.id,))
                )
                break
        else:
            # 只有相同服务/指标/标签、互不重叠的真实采样才可跨窗口比较。
            if len(samples) >= 2:
                old, new = samples[0], samples[-1]
                if old[1][-1][0] < new[1][0][0]:
                    baseline = sum(p[1] for p in old[1]) / len(old[1])
                    current = sum(p[1] for p in new[1]) / len(new[1])
                    if (
                        current * direction > threshold * direction
                        and (current - baseline) * direction >= delta
                    ):
                        findings.append(
                            SafetyFinding(
                                reason=AbortReason.METRICS_WORSENING,
                                evidence_ids=(old[0].id, new[0].id),
                            )
                        )
    for windows in impacts.values():
        windows.sort(key=lambda item: item[2])
        for prior, latest in zip(windows, windows[1:], strict=False):
            if prior[2] <= latest[1] and latest[3] > prior[3]:
                findings.append(
                    SafetyFinding(
                        reason=AbortReason.IMPACT_EXPANDING,
                        evidence_ids=(prior[0].id, latest[0].id),
                    )
                )
                break

    for fact in evidence:
        if fact.source != "reviewer.verdict":
            continue
        decision = ReviewDecision.model_validate_json(json.dumps(fact.result))
        references = decision.report.evidence_ids
        if (
            decision.report.verdict == "contradicted"
            and references <= successful
            and any(
                e.id == decision.conclusion_evidence_id and e.source == "agent.conclusion"
                for e in evidence
            )
        ):
            findings.append(
                SafetyFinding(
                    reason=AbortReason.EVIDENCE_CONFLICT,
                    evidence_ids=(
                        fact.id,
                        decision.conclusion_evidence_id,
                        *sorted(references, key=str),
                    ),
                )
            )

    # 计数以匹配后的每次独立诊断/验证为单位，Activity 重投、多个失败步骤不重复计数。
    matches: list[tuple[int, RunbookView, UUID]] = []
    outcomes: dict[UUID, list[tuple[bool, tuple[UUID, ...]]]] = defaultdict(list)
    seen_attempts: set[tuple[UUID, str, int]] = set()
    for fact in evidence:
        if (
            fact.source == "runbook.match"
            and fact.result.get("runbook_json") is not None
            and fact.result.get("blocked") is False
        ):
            guide = RunbookView.model_validate_json(str(fact.result["runbook_json"]))
            matches.append((int(str(fact.parameters["phase_version"])), guide, fact.id))
        elif fact.source in {"agent.observe", "verify_action"} and matches:
            if fact.source == "agent.observe":
                phase = int(str(fact.parameters["phase_version"]))
                failed = fact.result.get("status") == "failed"
                if not failed or not any(
                    str(a.id) == fact.result.get("audit_id") and a.outcome == "failed"
                    for a in audits
                ):
                    continue
                attempt_type = "diagnosis"
            else:
                if fact.id not in successful:
                    continue
                report = VerificationReport.model_validate_json(json.dumps(fact.result))
                phase, failed, attempt_type = (
                    report.spec.verifying_version,
                    not report.passed,
                    "verification",
                )
            candidates = [m for m in matches if m[0] < phase]
            if not candidates:
                continue
            _, guide, match_id = max(candidates, key=lambda m: m[0])
            attempt = (guide.id, attempt_type, phase)
            if attempt not in seen_attempts:
                seen_attempts.add(attempt)
                outcomes[guide.id].append((failed, (fact.id, match_id)))
    for results in outcomes.values():
        consecutive: list[tuple[UUID, ...]] = []
        for failed, outcome_references in results:
            if not failed:
                consecutive.clear()
            else:
                consecutive.append(outcome_references)
        if len(consecutive) >= config.max_consecutive_runbook_failures:
            findings.append(
                SafetyFinding(
                    reason=AbortReason.RUNBOOK_FAILURES,
                    evidence_ids=tuple(dict.fromkeys(r for refs in consecutive for r in refs)),
                )
            )

    intents = {
        (str(e.parameters.get("plan_evidence_id")), str(e.parameters.get("action_id"))): e.id
        for e in evidence
        if e.source == "execution.intent"
    }
    count = len(intents) + int(pending_action is not None and pending_action not in intents)
    if count > config.max_actions:
        findings.append(
            SafetyFinding(reason=AbortReason.ACTION_LIMIT, evidence_ids=tuple(intents.values()))
        )
    # 保留六种独立原因，每种首次判据即可；报告始终引用真实证据/失败调用审计。
    return tuple({f.reason: f for f in reversed(findings)}.values())
