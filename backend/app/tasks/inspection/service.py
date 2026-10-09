"""风险与报告同事务；任务锁和服务锁保证重试、并发与持续异常去重。"""

import json
from dataclasses import asdict
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.feishu.base import FeishuConnector
from app.connectors.feishu.models import TextNotification
from app.connectors.inspection.models import InspectionFacts
from app.db.base import utc_now
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.runbooks.schemas import MatchingFacts, RunbookSearch, applicability
from app.tasks.inspection.engine import evaluate
from app.tasks.inspection.models import (
    CheckResult,
    InspectionReport,
    InspectionRequest,
    InspectionResult,
    RiskEntry,
    risk_key,
)
from app.tasks.models import AITask
from app.tasks.states import TaskSource, TaskStatus
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus
from app.tools.registry import json_object
from app.tools.runbooks import SearchRunbooksOutput
from app.triggers.models import OpsEvent


async def cached(session: AsyncSession, task_id: UUID, source: str, key: object) -> Evidence | None:
    return next(
        (
            e
            for e in await LedgerService(session).evidence_for_task(task_id)
            if e.source_tool == source and e.parameters == key
        ),
        None,
    )


class InspectionService:
    def __init__(
        self, session: AsyncSession, settings: Settings, dispatcher: ToolDispatcher
    ) -> None:
        self.session, self.settings, self.dispatcher = session, settings, dispatcher
        self.ledger = LedgerService(session)

    async def scan(self, request: InspectionRequest) -> InspectionResult:
        key = json_object(asdict(request))
        saved = await cached(self.session, UUID(request.task.task_id), "inspection.report", key)
        if saved is not None:
            return await self.result(saved)
        task = await self.session.scalar(
            select(AITask)
            .where(AITask.id == UUID(request.task.task_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            raise ValueError("巡检任务不存在")
        saved = await cached(self.session, task.id, "inspection.report", key)
        if saved is not None:
            return await self.result(saved)
        event = await self.session.scalar(select(OpsEvent).where(OpsEvent.task_id == task.id))
        if (
            task.status is not TaskStatus.INVESTIGATING
            or task.status_version != request.task.version
            or task.source is not TaskSource.SCHEDULE
            or event is None
            or event.origin != "schedule"
            or not event.external_id.startswith(
                ("workday-inspection:", "hourly-capacity:", "daily-governance:")
            )
            or not self.settings.inspection_config.enabled
        ):
            raise ValueError("巡检只接受启用的定时 OpsEvent/统一任务入口")
        expected_modes = {
            "workday-inspection": "inspection",
            "hourly-capacity": "capacity",
            "daily-governance": "governance",
        }
        if expected_modes[event.external_id.partition(":")[0]] != request.mode:
            raise ValueError("巡检类型与来源事件不匹配")
        # 先锁任务再重新查询检查点；提交后丢响应和并发请求不会重复调用 Connector。
        saved = await cached(self.session, task.id, "inspection.report", key)
        if saved is not None:
            return await self.result(saved)
        checks: list[CheckResult] = []
        runbooks: list[UUID] = []
        for service in sorted(self.settings.inspection_config.services):
            query = RunbookSearch(query=f"{service} 巡检 治理", limit=10)
            search = await self.dispatcher.dispatch(
                task_id=task.id,
                tool_name="search_runbooks",
                parameters=json_object(query.model_dump(mode="json")),
                actor="inspection",
            )
            if search.evidence_id is not None:
                runbooks.append(search.evidence_id)
            if search.status is DispatchStatus.SUCCEEDED:
                matches = SearchRunbooksOutput.model_validate_json(json.dumps(search.result))
                # 适用条件必须核对；诊断仅允许本次固定 L0 查询，不自动执行处理步骤。
                decisions = [
                    applicability(
                        hit.runbook,
                        MatchingFacts(
                            service_name=service, title=task.title, task_source=task.source.value
                        ),
                    )
                    for hit in matches.matches
                ]
                await self.ledger.append_evidence(
                    task_id=task.id,
                    source_tool="inspection.runbook_matching",
                    parameters={"service_name": service, "phase_version": task.status_version},
                    result_snapshot={
                        "search_evidence_id": str(search.evidence_id),
                        "decisions": [list(d) for d in decisions],
                    },
                    collected_at=utc_now(),
                )
            dispatch = await self.dispatcher.dispatch(
                task_id=task.id,
                tool_name="query_inspection_facts",
                parameters={"service_name": service},
                actor="inspection",
            )
            facts = (
                InspectionFacts.model_validate_json(json.dumps(dispatch.result))
                if (dispatch.status is DispatchStatus.SUCCEEDED)
                else None
            )
            values = evaluate(
                service,
                request.mode,
                self.settings.inspection_config,
                facts,
                dispatch.evidence_id,
                utc_now(),
            )
            if search.status is not DispatchStatus.SUCCEEDED:
                values = tuple(
                    item.model_copy(
                        update={
                            "outcome": "unknown",
                            "reason": "Runbook 查询被拒或失败，巡检覆盖未确认",
                        }
                    )
                    for item in values
                )
            checks.extend(values)
        report = InspectionReport(
            task_id=task.id,
            phase_version=task.status_version,
            mode=request.mode,
            rule_fingerprint=self.settings.inspection_config.fingerprint,
            checks=tuple(checks),
            runbook_evidence_ids=tuple(runbooks),
        )
        # 多服务按固定锁顺序；不同巡检任务观察同一风险时也只打开一个通知周期。
        for service in sorted(self.settings.inspection_config.services):
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                {"scope": "inspection:" + service},
            )
        for check in report.checks:
            await self.record_risk(task.id, check)
        # 缺整项检查时使用服务范围作为占位资源；完整来源恢复后真实资源名可能不同。
        # 只清除无观测的 unknown 占位，不把真实资源消失解释为恢复。
        unresolved = list(
            await self.session.scalars(
                select(RiskEntry).where(
                    RiskEntry.service_name.in_(self.settings.inspection_config.services),
                    RiskEntry.active,
                    RiskEntry.outcome == "unknown",
                )
            )
        )
        for risk in unresolved:
            matching = [
                c
                for c in report.checks
                if c.service_name == risk.service_name and c.check_id == risk.check_id
            ]
            previous = await self.ledger.get_evidence(risk.latest_evidence_id)
            old_check = CheckResult.model_validate_json(json.dumps(previous.result_snapshot))
            if (
                matching
                and all(c.outcome != "unknown" for c in matching)
                and not any(c.resource == risk.resource for c in matching)
                and old_check.observed_at is None
            ):
                await self.record_risk(
                    task.id,
                    matching[0].model_copy(
                        update={
                            "resource": risk.resource,
                            "outcome": "healthy",
                            "observed_at": matching[0].assessed_at,
                            "reason": "来源完整性已恢复，缺失检查的占位风险已清除",
                        }
                    ),
                )
        saved = await self.ledger.append_evidence(
            task_id=task.id,
            source_tool="inspection.report",
            parameters=key,
            result_snapshot=json_object(report.model_dump(mode="json")),
            collected_at=utc_now(),
        )
        return await self.result(saved)

    async def record_risk(self, task_id: UUID, check: CheckResult) -> None:
        identity = risk_key(check)
        risk = await self.session.scalar(select(RiskEntry).where(RiskEntry.risk_key == identity))
        # unknown 的来源时间不可信（可能来自未来）；恢复时按本次核验时间解除 unknown。
        seen = (
            check.assessed_at
            if check.outcome == "unknown" or (risk is not None and risk.outcome == "unknown")
            else check.observed_at or check.assessed_at
        )
        if risk is not None and seen < risk.last_seen:
            return  # 乱序数据既不清除也不重开风险。
        if check.outcome == "healthy" and risk is None:
            return
        # 相同时间的冲突/缺失数据不能清除已有异常。
        if (
            risk is not None
            and seen == risk.last_seen
            and risk.active
            and risk.outcome == "abnormal"
            and check.outcome == "healthy"
        ):
            return
        # 不可靠或不完整的观测不刷新已知事实的时间，更不能清除已有异常。
        if risk is not None and check.outcome == "unknown":
            return
        record = await self.ledger.append_evidence(
            task_id=task_id,
            source_tool="inspection.risk",
            parameters={"risk_key": identity},
            result_snapshot=json_object(check.model_dump(mode="json")),
            source_reference=check.source_reference,
            collected_at=utc_now(),
        )
        if risk is None:
            risk = RiskEntry(
                risk_key=identity,
                service_name=check.service_name,
                check_id=check.check_id,
                resource=check.resource,
                category=check.category,
                outcome=check.outcome,
                active=True,
                episode=1,
                first_seen=seen,
                last_seen=seen,
                opening_evidence_id=record.id,
                latest_evidence_id=record.id,
            )
            self.session.add(risk)
        else:
            risk.last_seen, risk.latest_evidence_id = seen, record.id
            if check.outcome == "healthy":
                risk.active, risk.cleared_at = False, seen
            else:
                if not risk.active or risk.outcome != check.outcome:
                    risk.episode += 1
                    risk.opening_evidence_id, risk.notification_evidence_id = record.id, None
                risk.active, risk.cleared_at, risk.outcome = True, None, check.outcome
        await self.session.flush()

    async def result(self, record: Evidence) -> InspectionResult:
        report = InspectionReport.model_validate_json(json.dumps(record.result_snapshot))
        keys = [risk_key(c) for c in report.checks if c.outcome != "healthy"]
        risks = list(
            await self.session.scalars(select(RiskEntry).where(RiskEntry.risk_key.in_(keys)))
        )
        return InspectionResult(
            str(record.id), report.model_dump_json(), [str(r.id) for r in risks]
        )


async def notify_risk(session: AsyncSession, risk_id: UUID, connector: FeishuConnector) -> None:
    risk = await session.scalar(select(RiskEntry).where(RiskEntry.id == risk_id).with_for_update())
    if risk is None or not risk.active or risk.notification_evidence_id is not None:
        return
    ledger = LedgerService(session)
    opening = await ledger.get_evidence(risk.opening_evidence_id)
    check = CheckResult.model_validate_json(json.dumps(opening.result_snapshot))
    notification = TextNotification(
        notification_id=uuid5(NAMESPACE_URL, f"inspection:{risk.risk_key}:{risk.episode}"),
        text=f"巡检风险：{check.label}\n服务：{risk.service_name}\n资源：{risk.resource}\n"
        f"判定：{'需要补充信息' if check.outcome == 'unknown' else '异常'}\n"
        f"{check.reason}\nEvidence: {opening.id}\n事实 Evidence: {check.evidence_id or '缺失'}",
    )
    receipt = await connector.send(notification)
    evidence = await ledger.append_evidence(
        task_id=opening.task_id,
        source_tool="inspection.notification",
        parameters={"risk_id": str(risk.id), "episode": risk.episode},
        result_snapshot=json_object(receipt.model_dump(mode="json")),
        collected_at=utc_now(),
    )
    await ledger.append_audit(
        task_id=opening.task_id,
        event_type=AuditEventType.HUMAN_INTERACTION,
        actor="inspection",
        operation="inspection.notify",
        outcome="sent",
        evidence_id=evidence.id,
        details={"risk_id": str(risk.id), "notification_id": str(notification.notification_id)},
    )
    risk.notification_evidence_id = evidence.id
