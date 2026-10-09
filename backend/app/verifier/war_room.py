"""独立读取实际资源、容量与风险；执行成功不能直接关闭保障。"""

import json
from dataclasses import asdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.kubernetes.execution import ExecutionReceipt, KubernetesWriteConnector
from app.connectors.war_room.facts import WarRoomConnector, WarRoomQuery
from app.db.base import utc_now
from app.db.session import Database
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import RiskLevel
from app.tasks.inspection.service import cached
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus, TransitionActor, VerificationRequired
from app.tasks.war_room.engine import assess_facts
from app.tasks.war_room.models import WarRoomVerification, WarRoomVerified, WarRoomVerifyRequest
from app.tasks.war_room.service import (
    WarRoomService,
    audited_fact,
    load_assessment,
    owned_target,
    submission,
    validate_assessment_sources,
)
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus
from app.tools.registry import ToolRegistry, json_object
from app.tools.war_room import war_room_registry
from app.verifier.authority import _verification_scope


async def validate_war_room_verification(
    session: AsyncSession, task: AITask, evidence: Evidence, *, require_passed: bool
) -> WarRoomVerification:
    result = WarRoomVerification.model_validate_json(json.dumps(evidence.result_snapshot))
    if (
        evidence.task_id != task.id
        or evidence.source_tool != "verify_war_room"
        or result.task_id != task.id
        or result.verifying_version != task.status_version
        or (require_passed and (not result.passed or not result.final))
    ):
        raise VerificationRequired("最终保障核验任务、版本或结果无效")
    await audited_fact(session, task.id, evidence.id, "verify_war_room", "verifier")
    await audited_fact(
        session, task.id, result.target_evidence_id, "get_execution_target", "verifier"
    )
    await audited_fact(
        session, task.id, result.facts_evidence_id, "query_war_room_facts", "verifier"
    )
    assessment = await load_assessment(session, task, result.assessment_evidence_id)
    if result.final and assessment.purpose != "cleanup":
        raise VerificationRequired("只有结束后资源回收核验能关闭保障")
    return result


class WarRoomVerifier:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        facts: WarRoomConnector,
        resources: KubernetesWriteConnector,
    ) -> None:
        self.database, self.settings, self.facts, self.resources = (
            database,
            settings,
            facts,
            resources,
        )

    @activity.defn(name="verifier.war_room")
    async def verify(self, request: WarRoomVerifyRequest) -> WarRoomVerified:
        key = json_object(asdict(request))
        try:
            async with self.database.session() as session, session.begin():
                task = await session.scalar(
                    select(AITask)
                    .where(AITask.id == UUID(request.task.task_id))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if task is None:
                    raise VerificationRequired("保障核验任务不存在")
                ledger = LedgerService(session)
                saved = await cached(session, task.id, "war_room.verified", key)
                if saved and isinstance(saved.result_snapshot, dict):
                    return WarRoomVerified(
                        TaskSnapshot(
                            str(task.id),
                            TaskStatus(str(saved.result_snapshot["status"])),
                            int(str(saved.result_snapshot["version"])),
                        ),
                        str(saved.result_snapshot["evidence_id"]),
                        bool(saved.result_snapshot["passed"]),
                    )
                if (
                    task.status != TaskStatus.VERIFYING
                    or task.status_version != request.task.version
                ):
                    raise VerificationRequired("保障核验阶段或版本过期")
                assessment = await load_assessment(
                    session, task, UUID(request.assessment_evidence_id)
                )
                await validate_assessment_sources(session, task, assessment, self.settings)
                if assessment.purpose != ("cleanup" if request.final else "prepare"):
                    raise VerificationRequired("保障核验阶段不匹配")
                value = await submission(session, task)
                dispatcher = ToolDispatcher(
                    war_room_registry(session, self.settings, self.facts, self.resources),
                    create_policy_engine(self.settings),
                    ledger,
                )
                query = WarRoomQuery(
                    service_name=value.service_name,
                    purpose="verify" if request.final else "prepare",
                    start=utc_now(),
                    end=utc_now(),
                )
                raw = await WarRoomService(session, self.settings, dispatcher).query(
                    task, query, "verifier"
                )
                if raw is None:
                    failed = True
                else:
                    failed = False
                    facts_id, facts, target_id, target = raw
                    records = await ledger.evidence_for_task(task.id)
                    action_id = "war-room-cleanup" if request.final else "war-room-prepare"
                    executed = [
                        e
                        for e in records
                        if e.source_tool == "execute_action"
                        and e.parameters.get("action_id") == action_id
                    ]
                    expected = assessment.baseline if request.final else assessment.target
                    if executed:
                        if len(executed) != 1:
                            raise VerificationRequired("保障执行回执不唯一")
                        await audited_fact(
                            session, task.id, executed[0].id, "execute_action", "executor"
                        )
                        expected = ExecutionReceipt.model_validate_json(
                            json.dumps(executed[0].result_snapshot)
                        ).target
                    _, required, _, safe, complete, _, _ = assess_facts(
                        value,
                        self.settings.war_room_config,
                        self.settings.inspection_config,
                        facts,
                        target,
                        assessment.baseline,
                        facts_id,
                        utc_now(),
                        purpose="cleanup" if request.final else "prepare",
                        owned=target if request.final else await owned_target(session, task.id),
                    )
                    passed = bool(
                        safe
                        and complete
                        and target == expected
                        and target.replicas >= required
                        and (
                            not request.final
                            or (
                                utc_now() >= value.end
                                and target.replicas == assessment.baseline.replicas
                            )
                        )
                    )
                    result = WarRoomVerification(
                        task_id=task.id,
                        verifying_version=task.status_version,
                        assessment_evidence_id=UUID(request.assessment_evidence_id),
                        target_evidence_id=target_id,
                        facts_evidence_id=facts_id,
                        passed=passed,
                        final=request.final,
                    )
                    registry = ToolRegistry()

                    async def verify(query: WarRoomVerification) -> WarRoomVerification:
                        if query != result:
                            raise VerificationRequired("核验结果必须来自独立事实计算")
                        return result

                    registry.register(
                        name="verify_war_room",
                        description="独立验证保障资源和业务健康",
                        input_model=WarRoomVerification,
                        output_model=WarRoomVerification,
                        handler=verify,
                        risk_level=RiskLevel.L0,
                    )
                    dispatch = await ToolDispatcher(
                        registry, create_policy_engine(self.settings), ledger
                    ).dispatch(
                        task_id=task.id,
                        tool_name="verify_war_room",
                        parameters=result.model_dump(mode="json"),
                        actor="verifier",
                    )
                    if dispatch.status != DispatchStatus.SUCCEEDED or dispatch.evidence_id is None:
                        failed = True
                    else:
                        if request.final and passed:
                            with _verification_scope(
                                task.id, task.status_version, dispatch.evidence_id
                            ):
                                await TaskService(session).transition(
                                    task.id,
                                    TaskStatus.RESOLVED,
                                    expected_status=task.status,
                                    expected_version=task.status_version,
                                    reason="重大保障结束，资源回收及业务健康已独立核验",
                                    actor=TransitionActor.VERIFIER,
                                )
                        await ledger.append_evidence(
                            task_id=task.id,
                            source_tool="war_room.verified",
                            parameters=key,
                            result_snapshot={
                                "status": task.status.value,
                                "version": task.status_version,
                                "evidence_id": str(dispatch.evidence_id),
                                "passed": passed,
                            },
                        )
                        return WarRoomVerified(
                            TaskSnapshot(str(task.id), task.status, task.status_version),
                            str(dispatch.evidence_id),
                            passed,
                        )
            if failed:
                raise VerificationRequired("保障事实查询或聚合核验被拒，审计已保留")
            raise VerificationRequired("保障核验结果缺失")
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("重大保障独立核验被拒绝", non_retryable=True) from None
