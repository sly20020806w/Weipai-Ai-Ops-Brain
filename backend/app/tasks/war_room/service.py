"""保障经 OpsEvent 进入统一任务；只追加快照与检查点，重投不重复查询或建任务。"""

import json
import math
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.investigation import EvidenceClaim
from app.config import Settings
from app.connectors.kubernetes.execution import ExecutionReceipt
from app.connectors.war_room.facts import WarRoomFacts, WarRoomQuery
from app.db.base import utc_now
from app.executor.models import ExecutionTarget, TargetQuery
from app.executor.service import binding_for
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyAction, RiskLevel
from app.runbooks.schemas import MatchingFacts, RunbookSearch, applicability
from app.tasks.inspection.service import cached
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.planning.models import (
    ActionPlan,
    EvaluatedAction,
    PlannedAction,
    PlanningResult,
    RollbackPlan,
    VerificationPlan,
)
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.war_room.engine import assess_facts
from app.tasks.war_room.models import (
    SECTIONS,
    WarRoomAssessment,
    WarRoomRequest,
    WarRoomResult,
    WarRoomReview,
    WarRoomSubmission,
    WarRoomVerifyRequest,
)
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus, JsonObject
from app.tools.registry import json_object
from app.tools.runbooks import SearchRunbooksOutput
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventReceipt, NormalizedEvent
from app.triggers.service import EventService


async def submit_war_room(session: AsyncSession, value: WarRoomSubmission) -> EventReceipt:
    value = WarRoomSubmission.model_validate(value)
    # 同一服务的时间重叠保障不能争抢临时容量；检查与接纳在同一事务锁内。
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": "war-room:" + value.service_name},
    )
    records = await session.scalars(
        select(Evidence).where(
            Evidence.source_tool == "war_room.submission",
            Evidence.result_snapshot["service_name"].as_string() == value.service_name,
        )
    )
    for record in records:
        other = WarRoomSubmission.model_validate_json(json.dumps(record.result_snapshot))
        if (
            other.request_id != value.request_id
            and value.start < other.end
            and other.start < value.end
        ):
            raise ValueError("同一服务的保障时间不能重叠")
    receipt = (
        await EventService(session).accept(
            [
                NormalizedEvent(
                    origin="manual",
                    source=TaskSource.HUMAN,
                    external_id=f"war-room:{value.request_id}",
                    service_name=value.service_name,
                    title="重大保障：" + value.title,
                    occurred_at=value.start,
                )
            ]
        )
    )[0]
    ledger = LedgerService(session)
    previous = await cached(
        session, UUID(receipt.task_id), "war_room.submission", {"request_id": str(value.request_id)}
    )
    if previous is not None:
        if previous.result_snapshot != value.model_dump(mode="json"):
            raise ValueError("相同保障请求不得修改已提交材料")
    else:
        await ledger.append_evidence(
            task_id=UUID(receipt.task_id),
            source_tool="war_room.submission",
            parameters={"request_id": str(value.request_id)},
            result_snapshot=value.model_dump(mode="json"),
        )
    return receipt


async def submission(session: AsyncSession, task: AITask) -> WarRoomSubmission:
    records = [
        e
        for e in await LedgerService(session).evidence_for_task(task.id)
        if e.source_tool == "war_room.submission"
    ]
    event = await session.scalar(select(OpsEvent).where(OpsEvent.task_id == task.id))
    if len(records) != 1:
        raise ValueError("保障必须有唯一输入快照")
    value = WarRoomSubmission.model_validate_json(json.dumps(records[0].result_snapshot))
    if (
        task.source is not TaskSource.HUMAN
        or event is None
        or event.origin != "manual"
        or event.external_id != f"war-room:{value.request_id}"
        or event.service_name != value.service_name
        or event.occurred_at != value.start
        or task.title != "重大保障：" + value.title
    ):
        raise ValueError("保障与事件/任务身份不一致")
    return value


def config_hash(settings: Settings, service: str) -> str:
    return sha256(
        json.dumps(
            {
                "war_room": settings.war_room_config.model_dump(mode="json"),
                "inspection": settings.inspection_config.model_dump(mode="json"),
                "binding": binding_for(settings, service).model_dump(mode="json"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


async def audited_fact(
    session: AsyncSession, task_id: UUID, evidence_id: UUID, name: str, actor: str
) -> Evidence:
    ledger = LedgerService(session)
    fact = await ledger.get_evidence(evidence_id)
    if (
        fact.task_id != task_id
        or fact.source_tool != name
        or not any(
            a.event_type is AuditEventType.TOOL_CALL
            and a.evidence_id == fact.id
            and a.operation == name
            and a.actor == actor
            and a.outcome == "succeeded"
            and a.details.get("mode") == "live"
            for a in await ledger.audits_for_task(task_id)
        )
    ):
        raise ValueError("保障事实缺少同任务成功查询与操作人审计")
    return fact


async def load_assessment(
    session: AsyncSession, task: AITask, evidence_id: UUID
) -> WarRoomAssessment:
    record = await LedgerService(session).get_evidence(evidence_id)
    value = await submission(session, task)
    assessment = WarRoomAssessment.model_validate_json(json.dumps(record.result_snapshot))
    if (
        record.task_id != task.id
        or record.source_tool != "war_room.assessment"
        or assessment.task_id != task.id
        or assessment.submission_hash != value.digest
        or record.parameters.get("purpose") != assessment.purpose
        or not isinstance(record.parameters.get("task"), dict)
        or cast(dict[str, object], record.parameters["task"]).get("version")
        != assessment.phase_version
    ):
        raise ValueError("保障检查的任务、版本或输入不一致")
    return assessment


async def owned_target(session: AsyncSession, task_id: UUID) -> ExecutionTarget | None:
    records = await LedgerService(session).evidence_for_task(task_id)
    receipts = [
        e
        for e in records
        if e.source_tool == "execute_action" and e.parameters.get("action_id") == "war-room-prepare"
    ]
    if not receipts:
        return None
    if len(receipts) != 1:
        raise ValueError("资源准备回执不唯一")
    receipt = ExecutionReceipt.model_validate_json(json.dumps(receipts[0].result_snapshot))
    await audited_fact(session, task_id, receipts[0].id, "execute_action", "executor")
    return receipt.target


class WarRoomService:
    def __init__(
        self, session: AsyncSession, settings: Settings, dispatcher: ToolDispatcher
    ) -> None:
        self.session, self.settings, self.dispatcher = session, settings, dispatcher
        self.ledger = LedgerService(session)

    async def lock(self, snapshot: object) -> AITask:
        from app.tasks.workflow_models import TaskSnapshot

        if not isinstance(snapshot, TaskSnapshot):
            raise ValueError("保障任务快照无效")
        task = await self.session.scalar(
            select(AITask)
            .where(AITask.id == UUID(snapshot.task_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            raise ValueError("保障任务不存在")
        return task

    async def query(
        self, task: AITask, query: WarRoomQuery, actor: str
    ) -> tuple[UUID, WarRoomFacts, UUID, ExecutionTarget] | None:
        binding = binding_for(self.settings, query.service_name)
        results = []
        for name, parameters in (
            ("query_war_room_facts", query.model_dump(mode="json")),
            (
                "get_execution_target",
                TargetQuery(
                    **binding.model_dump(
                        include={
                            "service_name",
                            "cluster_name",
                            "namespace",
                            "deployment_name",
                            "container_name",
                        }
                    )
                ).model_dump(mode="json"),
            ),
        ):
            result = await self.dispatcher.dispatch(
                task_id=task.id, tool_name=name, parameters=parameters, actor=actor
            )
            if result.status is not DispatchStatus.SUCCEEDED or result.evidence_id is None:
                return None
            results.append(result)
        facts = WarRoomFacts.model_validate_json(json.dumps(results[0].result))
        target = ExecutionTarget.model_validate_json(json.dumps(results[1].result))
        if facts.query != query:
            raise ValueError("来源未返回精确保障采集窗口")
        assert results[0].evidence_id and results[1].evidence_id
        return results[0].evidence_id, facts, results[1].evidence_id, target

    async def assess(self, request: WarRoomRequest) -> WarRoomResult:
        task = await self.lock(request.task)
        key = json_object(asdict(request))
        previous = await cached(self.session, task.id, "war_room.assessment", key)
        if previous:
            assessment = WarRoomAssessment.model_validate_json(json.dumps(previous.result_snapshot))
            return WarRoomResult(
                str(previous.id), assessment.model_dump_json(), list(assessment.receipts)
            )
        value = await submission(self.session, task)
        purpose = cast(Literal["prepare", "watch", "cleanup"], request.purpose)
        if (
            not self.settings.war_room_config.enabled
            or purpose not in {"prepare", "watch", "cleanup"}
            or task.status_version != request.task.version
            or task.status != request.task.status
            or task.status != (TaskStatus.VERIFYING if purpose == "watch" else TaskStatus.RCA)
        ):
            raise ValueError("保障场景、版本或阶段无效")
        if (
            math.ceil(
                (value.end - value.start).total_seconds()
                / self.settings.war_room_config.interval_seconds
            )
            > self.settings.war_room_config.max_windows
        ):
            raise ValueError("保障窗口超过宿主盯盘次数上限")
        search = await self.dispatcher.dispatch(
            task_id=task.id,
            tool_name="search_runbooks",
            parameters=RunbookSearch(
                query=f"{value.service_name} 重大保障 容量", limit=100
            ).model_dump(mode="json"),
            actor="war-room",
        )
        if search.status is not DispatchStatus.SUCCEEDED or search.evidence_id is None:
            return await self.blocked(task, key)
        hits = SearchRunbooksOutput.model_validate_json(json.dumps(search.result))
        decisions = tuple(
            applicability(
                h.runbook,
                MatchingFacts(
                    service_name=value.service_name, title=task.title, task_source=task.source.value
                ),
            )
            for h in hits.matches
        )
        query = WarRoomQuery(
            service_name=value.service_name,
            purpose=purpose,
            window=request.window,
            start=value.start if request.start is None else datetime.fromisoformat(request.start),
            end=value.end if request.end is None else datetime.fromisoformat(request.end),
        )
        if purpose == "watch" and not (
            value.start <= query.start < query.end <= value.end and query.end <= utc_now()
        ):
            raise ValueError("盯盘必须查询已经完成且属于保障期的窗口")
        raw = await self.query(task, query, "war-room")
        if raw is None:
            return await self.blocked(task, key)
        facts_id, facts, target_id, target = raw
        preparations = [
            e
            for e in await self.ledger.evidence_for_task(task.id)
            if e.source_tool == "war_room.assessment" and e.parameters.get("purpose") == "prepare"
        ]
        baseline = (
            WarRoomAssessment.model_validate_json(
                json.dumps(preparations[0].result_snapshot)
            ).baseline
            if preparations
            else target
        )
        owned = await owned_target(self.session, task.id)
        now = utc_now()
        checks, required, known, safe, complete, anomaly, ownership = assess_facts(
            value,
            self.settings.war_room_config,
            self.settings.inspection_config,
            facts,
            target,
            baseline,
            facts_id,
            now,
            purpose=purpose,
            owned=owned,
        )
        complete = complete and bool(decisions) and any(d[0] for d in decisions)
        safe = safe and complete
        if purpose == "prepare":
            safe = (
                safe
                and value.end > now
                and required <= binding_for(self.settings, value.service_name).max_replicas
            )
            required = max(target.replicas, required)
        receipts: tuple[EventReceipt, ...] = ()
        if purpose == "watch" and (anomaly or not complete):
            # 持续同类异常只建一条处置任务；每个窗口仍独立留证。
            receipt = (
                await EventService(self.session).accept(
                    [
                        NormalizedEvent(
                            origin="manual",
                            source=TaskSource.ALERT,
                            external_id=f"war-room-anomaly:{value.request_id}",
                            service_name=value.service_name,
                            title="重大保障异常：" + value.title,
                            occurred_at=value.start,
                        )
                    ]
                )
            )[0]
            await self.ledger.append_evidence(
                task_id=UUID(receipt.task_id),
                source_tool="war_room.anomaly",
                parameters={"parent_task_id": str(task.id), "window": request.window},
                result_snapshot={
                    "facts_evidence_id": str(facts_id),
                    "target_evidence_id": str(target_id),
                    "unknown": not complete,
                    "service_name": value.service_name,
                },
            )
            receipts = (receipt,)
        assessment = WarRoomAssessment(
            task_id=task.id,
            phase_version=task.status_version,
            purpose=purpose,
            submission_hash=value.digest,
            config_hash=config_hash(self.settings, value.service_name),
            assessed_at=now,
            target=target,
            baseline=baseline,
            required_replicas=required,
            runbook_evidence_id=search.evidence_id,
            runbook_decisions=decisions,
            facts_evidence_id=facts_id,
            target_evidence_id=target_id,
            checks=checks,
            capacity_known=known,
            safe=safe,
            complete=complete,
            anomaly=anomaly,
            ownership_matches=ownership,
            receipts=receipts,
        )
        record = await self.ledger.append_evidence(
            task_id=task.id,
            source_tool="war_room.assessment",
            parameters=key,
            result_snapshot=assessment.model_dump(mode="json"),
        )
        return WarRoomResult(str(record.id), assessment.model_dump_json(), list(receipts))

    async def blocked(self, task: AITask, key: JsonObject) -> WarRoomResult:
        record = await self.ledger.append_evidence(
            task_id=task.id,
            source_tool="war_room.blocked",
            parameters=key,
            result_snapshot={"reason": "保障查询被 Policy 拒绝或失败"},
        )
        return WarRoomResult(str(record.id), "{}", [], True)

    async def review(self, request: WarRoomVerifyRequest) -> str:
        task = await self.lock(request.task)
        key = json_object(asdict(request))
        previous = await cached(self.session, task.id, "war_room.review", key)
        if previous:
            return str(previous.id)
        assessment = await load_assessment(self.session, task, UUID(request.assessment_evidence_id))
        if (
            task.status != TaskStatus.RCA
            or task.status_version != request.task.version
            or assessment.phase_version != task.status_version
        ):
            raise ValueError("Reviewer 必须绑定当前保障检查")
        raw = await self.query(
            task,
            WarRoomQuery(
                service_name=assessment.target.service_name,
                purpose=assessment.purpose,
                start=utc_now(),
                end=utc_now(),
            ),
            "war-room-reviewer",
        )
        if raw is None:
            return ""  # 查询拒绝审计提交后父 Workflow 转人工。
        facts_id, facts, target_id, target = raw
        value = await submission(self.session, task)
        _, required, _, safe, complete, _, owned = assess_facts(
            value,
            self.settings.war_room_config,
            self.settings.inspection_config,
            facts,
            target,
            assessment.baseline,
            facts_id,
            utc_now(),
            purpose=assessment.purpose,
            owned=await owned_target(self.session, task.id),
        )
        review = WarRoomReview(
            task_id=task.id,
            phase_version=task.status_version,
            assessment_evidence_id=UUID(request.assessment_evidence_id),
            target_evidence_id=target_id,
            facts_evidence_id=facts_id,
            alternatives=(
                "容量假设不足",
                "监控或风险遗漏",
                "资源已被其他变更修改",
                "回收后容量不足",
            ),
            clear=bool(
                assessment.safe
                and safe
                and complete
                and target == assessment.target
                and (
                    assessment.purpose != "prepare"
                    or max(target.replicas, required) == assessment.required_replicas
                )
                and assessment.config_hash == config_hash(self.settings, value.service_name)
                and (
                    assessment.purpose != "cleanup"
                    or (owned and required <= assessment.baseline.replicas)
                )
            ),
        )
        record = await self.ledger.append_evidence(
            task_id=task.id,
            source_tool="war_room.review",
            parameters=key,
            result_snapshot=review.model_dump(mode="json"),
        )
        return str(record.id)

    async def plan(self, request: WarRoomVerifyRequest) -> PlanningResult:
        task = await self.lock(request.task)
        assessment = await load_assessment(self.session, task, UUID(request.assessment_evidence_id))
        review = await require_war_room_review(self.session, task)
        if (
            review is None
            or task.status != TaskStatus.PLANNING
            or task.status_version != request.task.version
        ):
            raise ValueError("保障计划必须有当前 Reviewer 放行")
        records = await self.ledger.evidence_for_task(task.id)
        review_record = next(
            e
            for e in records
            if e.source_tool == "war_room.review"
            and e.result_snapshot == review.model_dump(mode="json")
        )
        key: JsonObject = {
            "phase_version": task.status_version,
            "conclusion_evidence_id": request.assessment_evidence_id,
            "review_evidence_id": str(review_record.id),
        }
        previous = await cached(self.session, task.id, "action_plan", key)
        if previous:
            return PlanningResult(
                str(previous.id),
                ActionPlan.model_validate_json(
                    json.dumps(previous.result_snapshot)
                ).model_dump_json(),
            )
        destination = (
            assessment.required_replicas
            if assessment.purpose == "prepare"
            else assessment.baseline.replicas
        )
        claim = EvidenceClaim(
            statement="保障容量与风险检查及独立反证通过，逐项提交 Policy。",
            evidence_ids=(UUID(request.assessment_evidence_id), review_record.id),
        )
        action = PlannedAction(
            id="war-room-" + assessment.purpose,
            name="scale_service",
            service_name=assessment.target.service_name,
            parameters={"from_replicas": assessment.target.replicas, "to_replicas": destination},
            risk_level=RiskLevel.L3,
            rationale=claim,
            preconditions=("保障时间、资源 UID/版本、镜像、容量和宿主规则仍与检查一致",),
            rollback=RollbackPlan(
                description="停止自动化，重新审批恢复操作前副本数",
                parameters={
                    "from_replicas": destination,
                    "to_replicas": assessment.target.replicas,
                },
                trigger="业务指标恶化或目标未就绪",
            ),
            verification=VerificationPlan(
                checks=("资源精确读回及就绪副本", "业务健康与监控风险", "回收后容量余量"),
                success_criteria="资源达到批准副本数且独立业务/风险/容量检查均通过",
                failure_response="停止自动尝试并转人工，不把执行成功当作保障完成",
            ),
        )
        policy = create_policy_engine(self.settings)
        plan = ActionPlan(
            task_id=task.id,
            planning_version=task.status_version,
            conclusion_evidence_id=UUID(request.assessment_evidence_id),
            review_evidence_id=review_record.id,
            environment=policy.environment,
            summary=claim,
            actions=(
                EvaluatedAction(
                    action=action,
                    policy=policy.evaluate(PolicyAction(name=action.name, risk_level=RiskLevel.L3)),
                ),
            ),
        )
        await require_war_room_plan(self.session, task, plan, self.settings)
        record = await self.ledger.append_evidence(
            task_id=task.id,
            source_tool="action_plan",
            parameters=key,
            result_snapshot=plan.model_dump(mode="json"),
        )
        return PlanningResult(str(record.id), plan.model_dump_json())

    async def report(self, snapshot: object) -> str:
        task = await self.lock(snapshot)
        if task.status != TaskStatus.LEARNING:
            raise ValueError("保障报告只在独立验证成功后生成")
        key: JsonObject = {"version": task.status_version}
        previous = await cached(self.session, task.id, "war_room.report", key)
        if previous:
            return str(previous.id)
        records = await self.ledger.evidence_for_task(task.id)
        assessments = [e for e in records if e.source_tool == "war_room.assessment"]
        verification = [e for e in records if e.source_tool == "verify_war_room"]
        if (
            not verification
            or not isinstance(verification[-1].result_snapshot, dict)
            or not verification[-1].result_snapshot.get("passed")
            or not verification[-1].result_snapshot.get("final")
        ):
            raise ValueError("保障报告缺少成功的最终独立核验")
        first_record = next(
            e
            for e in assessments
            if e.parameters.get("purpose") == "prepare"
            and isinstance(e.result_snapshot, dict)
            and e.result_snapshot.get("safe")
        )
        first = WarRoomAssessment.model_validate_json(json.dumps(first_record.result_snapshot))
        last = WarRoomAssessment.model_validate_json(json.dumps(assessments[-1].result_snapshot))
        windows = [e for e in assessments if e.parameters.get("purpose") == "watch"]
        anomaly_tasks = sorted(
            {
                str(r.task_id)
                for e in assessments
                for r in WarRoomAssessment.model_validate_json(
                    json.dumps(e.result_snapshot)
                ).receipts
            }
        )
        execution = [e for e in records if e.source_tool == "execute_action"]
        input_value = await submission(self.session, task)
        summary = await self.ledger.append_evidence(
            task_id=task.id,
            source_tool="war_room.monitoring_summary",
            parameters=key,
            result_snapshot=json_object(
                {
                    "window_evidence_ids": [str(e.id) for e in windows],
                    "abnormal_windows": [
                        str(e.id)
                        for e in windows
                        if isinstance(e.result_snapshot, dict) and e.result_snapshot.get("anomaly")
                    ],
                    "unknown_windows": [
                        str(e.id)
                        for e in windows
                        if isinstance(e.result_snapshot, dict)
                        and not e.result_snapshot.get("complete")
                    ],
                    "anomaly_task_ids": anomaly_tasks,
                }
            ),
        )
        preparation = tuple(
            e.id for e in execution if e.parameters.get("action_id") == "war-room-prepare"
        )
        cleanup = tuple(
            e.id for e in execution if e.parameters.get("action_id") == "war-room-cleanup"
        )
        citations = (
            (first_record.id, first.facts_evidence_id),
            preparation + (verification[0].id,),
            (first.facts_evidence_id,),
            (first.facts_evidence_id, summary.id),
            (first.runbook_evidence_id,),
            (first.facts_evidence_id,),
            (first_record.id, assessments[-1].id),
            (summary.id,),
            (summary.id,),
            cleanup + (verification[-1].id,),
            (first_record.id, summary.id, verification[-1].id),
        )
        findings = (
            f"预计峰值 {input_value.projected_rps:g} RPS，需 {first.required_replicas} 副本；"
            f"保障前 {first.baseline.replicas} 副本。",
            "授权资源准备动作数："
            f"{sum(e.parameters.get('action_id') == 'war-room-prepare' for e in execution)}；"
            "准备结果经独立核验。",
            "监控覆盖："
            + next(c.outcome for c in first.checks if c.check_id == "monitoring_present"),
            "保障前告警："
            + next(c.outcome for c in first.checks if c.check_id == "alerts_healthy"),
            f"检索后适用 Runbook {sum(d[0] for d in first.runbook_decisions)} 个，"
            "已核对适用及排除条件；写入仍需单独授权。",
            "回滚准备事实已核对；恢复原副本数需新计划和独立审批，旧审批不可复用。",
            f"稳定性、容量、安全、成本共 {len(first.checks)} 项检查；准备与回收阶段均已通过。",
            f"完成 {len(windows)} 个 UTC 保障窗口，每个窗口有独立事实与风险评估证据。",
            f"创建 {len(anomaly_tasks)} 个去重处置任务，继续使用统一任务引擎；"
            "这些处置任务的结果须单独核验。",
            f"保障结束后资源恢复到 {last.baseline.replicas} 副本；"
            f"剩余负载所需 {last.required_replicas} 副本，业务健康及目标均已独立验证。",
            "重大保障独立核验通过；准备、盯盘、异常派发、授权动作及回收全部可追踪。",
        )
        record = await self.ledger.append_evidence(
            task_id=task.id,
            source_tool="war_room.report",
            parameters=key,
            result_snapshot=json_object(
                {
                    "task_id": str(task.id),
                    "submission": (await submission(self.session, task)).model_dump(mode="json"),
                    "sections": [
                        EvidenceClaim(
                            statement=finding,
                            evidence_ids=references,
                        ).model_dump(mode="json")
                        | {"name": name}
                        for name, finding, references in zip(
                            SECTIONS, findings, citations, strict=True
                        )
                    ],
                    "executions": [str(e.id) for e in records if e.source_tool == "execute_action"],
                    "approvals": [
                        str(e.id) for e in records if e.source_tool == "approval.decision"
                    ],
                    "verification": str(verification[-1].id),
                    "anomaly_tasks": anomaly_tasks,
                    "window_evidence_ids": [str(e.id) for e in windows],
                    "closed_at": utc_now().isoformat(),
                }
            ),
        )
        return str(record.id)


async def require_war_room_review(session: AsyncSession, task: AITask) -> WarRoomReview | None:
    records = await LedgerService(session).evidence_for_task(task.id)
    if not any(e.source_tool == "war_room.submission" for e in records):
        return None
    latest = await session.scalar(
        select(TaskStatusHistory)
        .where(
            TaskStatusHistory.task_id == task.id,
            TaskStatusHistory.to_status.in_([TaskStatus.RCA, TaskStatus.INVESTIGATING]),
        )
        .order_by(TaskStatusHistory.sequence.desc())
        .limit(1)
    )
    if latest is None or latest.to_status != TaskStatus.RCA:
        raise ValueError("保障动作必须有当前 RCA 的独立 Reviewer")
    reviews = [
        e
        for e in records
        if e.source_tool == "war_room.review"
        and isinstance(e.result_snapshot, dict)
        and e.result_snapshot.get("phase_version") == latest.sequence
    ]
    if len(reviews) != 1:
        raise ValueError("当前保障复核缺失或不唯一")
    review = WarRoomReview.model_validate_json(json.dumps(reviews[0].result_snapshot))
    assessment = await load_assessment(session, task, review.assessment_evidence_id)
    if (
        not review.clear
        or not assessment.safe
        or review.task_id != task.id
        or assessment.phase_version != latest.sequence
    ):
        raise ValueError("保障检查或独立反证不允许动作")
    for name, reference in (
        ("query_war_room_facts", review.facts_evidence_id),
        ("get_execution_target", review.target_evidence_id),
    ):
        await audited_fact(session, task.id, reference, name, "war-room-reviewer")
    for name, reference in (
        ("query_war_room_facts", assessment.facts_evidence_id),
        ("get_execution_target", assessment.target_evidence_id),
        ("search_runbooks", assessment.runbook_evidence_id),
    ):
        await audited_fact(session, task.id, reference, name, "war-room")
    return review


async def require_war_room_plan(
    session: AsyncSession, task: AITask, plan: ActionPlan, settings: Settings
) -> None:
    review = await require_war_room_review(session, task)
    if (
        review is None
        or not settings.war_room_config.enabled
        or settings.app_env not in {"local", "test"}
        or settings.connector_mode.value != "fake"
    ):
        raise PermissionError("重大保障动作仅开放启用的本机 Fake")
    value = await submission(session, task)
    assessment = await load_assessment(session, task, plan.conclusion_evidence_id)
    await validate_assessment_sources(session, task, assessment, settings)
    review_record = await LedgerService(session).get_evidence(plan.review_evidence_id)
    destination = (
        assessment.required_replicas
        if assessment.purpose == "prepare"
        else assessment.baseline.replicas
    )
    if (
        assessment.config_hash != config_hash(settings, value.service_name)
        or assessment.phase_version != plan.planning_version - 1
        or assessment.purpose not in {"prepare", "cleanup"}
        or review_record.task_id != task.id
        or review_record.source_tool != "war_room.review"
        or review_record.result_snapshot != review.model_dump(mode="json")
        or review.assessment_evidence_id != plan.conclusion_evidence_id
        or len(plan.actions) != 1
        or (
            plan.actions[0].action.id,
            plan.actions[0].action.name,
            plan.actions[0].action.service_name,
            plan.actions[0].action.risk_level,
            plan.actions[0].action.parameters,
        )
        != (
            "war-room-" + assessment.purpose,
            "scale_service",
            value.service_name,
            RiskLevel.L3,
            {"from_replicas": assessment.target.replicas, "to_replicas": destination},
        )
    ):
        raise PermissionError("保障计划改变了已核对的容量、阶段、资源或规则")
    age = (utc_now() - assessment.assessed_at).total_seconds()
    if not 0 <= age <= settings.inspection_config.max_age_seconds:
        raise PermissionError("保障授权依据已过期，需重新检查与审批")
    if assessment.purpose == "cleanup" and (
        utc_now() < value.end or not assessment.ownership_matches
    ):
        raise PermissionError("未结束保障或非本次准备的资源禁止回收")
    if assessment.purpose == "prepare" and utc_now() >= value.end:
        raise PermissionError("保障期已结束，旧准备审批失效")


async def validate_assessment_sources(
    session: AsyncSession,
    task: AITask,
    assessment: WarRoomAssessment,
    settings: Settings,
) -> None:
    value = await submission(session, task)
    source = await audited_fact(
        session, task.id, assessment.facts_evidence_id, "query_war_room_facts", "war-room"
    )
    target_record = await audited_fact(
        session, task.id, assessment.target_evidence_id, "get_execution_target", "war-room"
    )
    runbook_record = await audited_fact(
        session, task.id, assessment.runbook_evidence_id, "search_runbooks", "war-room"
    )
    facts = WarRoomFacts.model_validate_json(json.dumps(source.result_snapshot))
    target = ExecutionTarget.model_validate_json(json.dumps(target_record.result_snapshot))
    hits = SearchRunbooksOutput.model_validate_json(json.dumps(runbook_record.result_snapshot))
    decisions = tuple(
        applicability(
            h.runbook,
            MatchingFacts(
                service_name=value.service_name, title=task.title, task_source=task.source.value
            ),
        )
        for h in hits.matches
    )
    records = await LedgerService(session).evidence_for_task(task.id)
    first = next(
        e
        for e in records
        if e.source_tool == "war_room.assessment" and e.parameters.get("purpose") == "prepare"
    )
    first_assessment = await load_assessment(session, task, first.id)
    original_target = await audited_fact(
        session, task.id, first_assessment.target_evidence_id, "get_execution_target", "war-room"
    )
    baseline = ExecutionTarget.model_validate_json(json.dumps(original_target.result_snapshot))
    if first_assessment.target != baseline or first_assessment.baseline != baseline:
        raise PermissionError("保障前副本基线必须来自首次真实资源读回")
    checks, required, known, safe, complete, anomaly, ownership = assess_facts(
        value,
        settings.war_room_config,
        settings.inspection_config,
        facts,
        target,
        baseline,
        source.id,
        assessment.assessed_at,
        purpose=assessment.purpose,
        owned=None if assessment.purpose == "prepare" else await owned_target(session, task.id),
    )
    complete = complete and bool(decisions) and any(d[0] for d in decisions)
    safe = safe and complete
    if assessment.purpose == "prepare":
        safe = (
            safe
            and value.end > assessment.assessed_at
            and required <= binding_for(settings, value.service_name).max_replicas
        )
        required = max(target.replicas, required)
    if (
        source.parameters != facts.query.model_dump(mode="json")
        or facts.query.purpose != assessment.purpose
        or facts.query.service_name != value.service_name
        or assessment.config_hash != config_hash(settings, value.service_name)
        or assessment.target != target
        or assessment.baseline != baseline
        or assessment.runbook_decisions != decisions
        or (
            assessment.checks,
            assessment.required_replicas,
            assessment.capacity_known,
            assessment.safe,
            assessment.complete,
            assessment.anomaly,
            assessment.ownership_matches,
        )
        != (checks, required, known, safe, complete, anomaly, ownership)
        or runbook_record.collected_at > source.collected_at
    ):
        raise PermissionError("保障评估不能由伪造结论替代真实查询事实或先行 Runbook 检查")
