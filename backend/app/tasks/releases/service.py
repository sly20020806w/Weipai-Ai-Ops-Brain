"""发布证据检查、独立反证和规划；只通过高级 Tool 获取源事实。"""

import json
import re
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.activities import lock_task
from app.agent.fake import FakeLLM, ScriptedChatStep
from app.agent.investigation import EvidenceClaim
from app.agent.models import ChatMessage, ChatRequest, ChatResponse
from app.config import Settings
from app.connectors.changes.factory import (
    create_argocd_connector,
    create_ci_connector,
    create_config_center_connector,
    create_git_connector,
)
from app.connectors.changes.releases import FakeReleaseReader, FakeReleaseState
from app.db.base import utc_now
from app.db.session import Database
from app.executor.service import binding_for
from app.graph.service import GraphService
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.policy.models import PolicyAction, RiskLevel
from app.runbooks.lifecycle import task_runbook_context
from app.tasks.models import AITask, TaskStatusHistory
from app.tasks.planning.models import (
    ActionPlan,
    EvaluatedAction,
    PlannedAction,
    PlanningResult,
    RollbackPlan,
    VerificationPlan,
)
from app.tasks.releases.models import (
    ReleaseAssessment,
    ReleaseCheck,
    ReleaseManifest,
    ReleaseObservation,
    ReleasePlanRequest,
    ReleaseStageRequest,
    manifest_hash,
)
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.tickets.service import cached
from app.tools.changes import CompareVersionsOutput, register_change_tools
from app.tools.dispatcher import ToolDispatcher
from app.tools.graph import GraphContext, register_graph_tools
from app.tools.models import DispatchStatus, JsonObject, ToolModel
from app.tools.registry import ToolRegistry, json_object
from app.tools.releases import register_release_reads
from app.triggers.models import OpsEvent


def configuration_hash(settings: Settings) -> str:
    import hashlib

    return hashlib.sha256(settings.release_config.model_dump_json().encode()).hexdigest()


def recovery_checks(observation: ReleaseObservation, settings: Settings) -> dict[str, bool]:
    config = settings.release_config
    return {
        "deployment": observation.deployment_ready,
        "pods": observation.pods_ready,
        "http_5xx": len(observation.http_5xx) >= 3
        and max(observation.http_5xx) <= config.max_5xx_ratio,
        "p99": len(observation.p99_ms) >= 3 and max(observation.p99_ms) <= config.max_p99_ms,
        "success_ratio": len(observation.success_ratio) >= 3
        and min(observation.success_ratio) >= config.min_success_ratio,
        "logs": observation.logs_healthy,
        "traces": observation.traces_healthy,
        "resources": observation.resources_healthy,
    }


def sql_risk(manifest: ReleaseManifest, diff: CompareVersionsOutput) -> RiskLevel:
    statements = list(manifest.sql)
    for file in diff.code.files:
        added = "\n".join(
            line[1:]
            for line in file.patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        if (
            file.new_path.lower().endswith(".sql")
            or file.old_path.lower().endswith(".sql")
            or re.search(
                r"\b(?:DROP|TRUNCATE|DELETE|UPDATE|INSERT|ALTER|CREATE|GRANT|REVOKE)\b",
                added,
                flags=re.IGNORECASE,
            )
        ):
            statements.append(added)
    if not statements:
        return RiskLevel.L0
    if any(re.search(r"\b(?:DROP|TRUNCATE)\b", text, flags=re.IGNORECASE) for text in statements):
        return RiskLevel.L5
    return RiskLevel.L4


class ReleaseReview(ToolModel):
    task_id: UUID
    phase_version: int
    assessment_evidence_id: UUID
    clear: bool
    confidence: float
    alternatives: tuple[EvidenceClaim, ...]
    evidence_ids: tuple[UUID, ...]


def review_response(request: ChatRequest) -> ChatResponse:
    data = json.loads(request.messages[1].content or "{}")
    ids = tuple(UUID(i) for i in data["evidence_ids"])
    result = ReleaseReview(
        task_id=UUID(data["task_id"]),
        phase_version=data["phase_version"],
        assessment_evidence_id=UUID(data["assessment_evidence_id"]),
        clear=data["clear"],
        confidence=0.8 if data["clear"] else 0.5,
        alternatives=tuple(
            EvidenceClaim(statement=text, evidence_ids=ids)
            for text in (
                "复核源平台材料与版本 Diff，检查发布或 SQL 风险是否遗漏。",
                "复核实际上下游，确认网络和第三方影响面已纳入监控范围。",
                "复核资源和缓存相关图关系；图关系不能替代资源恢复检查。",
                "独立重读窗口指标、日志和 Trace；发布暂停和回滚不能声称已经恢复。",
            )
        ),
        evidence_ids=ids,
    )
    return ChatResponse(
        id="release-review",
        model="fake-release-reviewer",
        finish_reason="stop",
        message=ChatMessage(role="assistant", content=result.model_dump_json()),
    )


class ReleaseStore:
    def __init__(self, database: Database, settings: Settings, state: FakeReleaseState) -> None:
        self.database, self.settings, self.state = database, settings, state

    def validate_environment(self) -> None:
        if (
            not self.settings.release_config.enabled
            or self.settings.app_env not in {"local", "test"}
            or self.settings.connector_mode.value != "fake"
            or self.settings.llm_mode != "fake"
        ):
            raise PermissionError("发布场景仅开放本机 Fake，真实灰度及动作端协议尚未验收")

    async def read(
        self, session: AsyncSession, task_id: UUID, name: str, params: JsonObject, actor: str
    ) -> Evidence:
        self.validate_environment()
        async with AsyncExitStack() as stack:
            registry = ToolRegistry()
            register_release_reads(registry, FakeReleaseReader(self.state))
            register_graph_tools(registry, GraphService(session))
            register_change_tools(
                registry,
                await stack.enter_async_context(create_git_connector(self.settings)),
                await stack.enter_async_context(create_ci_connector(self.settings)),
                await stack.enter_async_context(create_argocd_connector(self.settings)),
                await stack.enter_async_context(create_config_center_connector(self.settings)),
            )
            result = await ToolDispatcher(
                registry, create_policy_engine(self.settings), LedgerService(session)
            ).dispatch(task_id=task_id, tool_name=name, parameters=params, actor=actor)
        if result.status is not DispatchStatus.SUCCEEDED or result.evidence_id is None:
            raise PermissionError("发布事实查询被拒绝或数据缺失")
        return await LedgerService(session).get_evidence(result.evidence_id)

    async def facts(
        self, session: AsyncSession, task_id: UUID, release_id: str, actor: str
    ) -> tuple[ReleaseManifest, list[Evidence]]:
        manifest_record = await self.read(
            session, task_id, "get_release_request", {"release_id": release_id}, actor
        )
        manifest = ReleaseManifest.model_validate_json(json.dumps(manifest_record.result_snapshot))
        end = utc_now()
        records = [
            manifest_record,
            await self.read(
                session,
                task_id,
                "compare_versions",
                {
                    "service_name": manifest.service_name,
                    "from_version": manifest.from_version,
                    "to_version": manifest.to_version,
                },
                actor,
            ),
            await self.read(
                session,
                task_id,
                "get_service_context",
                {"service_name": manifest.service_name},
                actor,
            ),
            await self.read(
                session, task_id, "get_dependencies", {"service_name": manifest.service_name}, actor
            ),
            await self.read(
                session,
                task_id,
                "query_release_observation",
                {
                    "release_id": release_id,
                    "start": (end - timedelta(seconds=1)).isoformat(),
                    "end": end.isoformat(),
                },
                actor,
            ),
        ]
        return manifest, records

    def checks(
        self, manifest: ReleaseManifest, records: list[Evidence], purpose: str
    ) -> tuple[ReleaseCheck, ...]:
        diff = CompareVersionsOutput.model_validate_json(json.dumps(records[1].result_snapshot))
        graph = GraphContext.model_validate_json(json.dumps(records[2].result_snapshot))
        dependencies = GraphContext.model_validate_json(json.dumps(records[3].result_snapshot))
        observation = ReleaseObservation.model_validate_json(json.dumps(records[4].result_snapshot))
        binding = binding_for(self.settings, manifest.service_name)
        # 所有 SQL 变更均需额外人工评审，不能用模型或正则“安全”判定授予 SQL 权限。
        sql_level = sql_risk(manifest, diff)
        sql_clear = sql_level is RiskLevel.L0
        impact_names = ", ".join(n.name for n in dependencies.nodes)
        values = [
            (
                "git_diff",
                bool(diff.code.files)
                and all(f.content_available for f in diff.code.files)
                and diff.code.comparison_kind == "direct",
                "已读取实际代码与配置 Diff；连接池等参数变化必须结合灰度指标验证。",
                (records[1].id,),
            ),
            (
                "impact",
                bool(graph.edges)
                and bool(dependencies.edges)
                and all(
                    e.confidence >= 0.9 and e.freshness_seconds <= 3600
                    for e in (*graph.edges, *dependencies.edges)
                ),
                "影响面：" + impact_names + "；图关系包含来源、置信度和新鲜度。",
                (records[2].id, records[3].id),
            ),
            (
                "sql",
                sql_clear,
                "SQL 检查：无 SQL 变更。"
                if sql_clear
                else f"SQL 检查：需人工评审（{sql_level.value}，至少 L4），阻止发布。",
                (records[0].id, records[1].id),
            ),
            (
                "resources",
                manifest.resource_ready and observation.resources_healthy,
                "资源余量与发布材料核对。",
                (records[0].id, records[4].id),
            ),
            (
                "monitoring",
                manifest.monitoring_ready
                and (
                    all(recovery_checks(observation, self.settings).values())
                    or purpose in {"pause", "rollback"}
                ),
                "监控、5xx、P99、成功率、日志和 Trace 基线已核对；异常处置不宣称恢复。",
                (records[0].id, records[4].id),
            ),
            (
                "rollback",
                manifest.rollback_ready
                and manifest.from_version in binding.images
                and manifest.to_version in binding.images,
                "回滚目标版本及宿主镜像白名单已核对。",
                (records[0].id,),
            ),
        ]
        return tuple(
            ReleaseCheck.model_validate_json(
                json.dumps(
                    {
                        "name": name,
                        "risk_level": sql_level.value if name == "sql" else "L0",
                        "passed": passed,
                        "claim": {"statement": text, "evidence_ids": [str(i) for i in ids]},
                    }
                )
            )
            for name, passed, text, ids in values
        )

    async def assess(self, request: ReleaseStageRequest) -> str:
        key = json_object(asdict(request))
        async with self.database.session() as session, session.begin():
            task = await lock_task(session, request.task)
            if task.status is not TaskStatus.RCA:
                raise ValueError("发布检查证据必须在 RCA")
            ledger = LedgerService(session)
            previous = await cached(session, task.id, "release.assessment", key)
            if previous:
                return str(previous.id)
            event = await session.scalar(select(OpsEvent).where(OpsEvent.task_id == task.id))
            manifest, records = await self.facts(
                session, task.id, request.release_id, "codex-main-agent"
            )
            if (
                event is None
                or task.source is not TaskSource.RELEASE
                or event.external_id != request.release_id
                or event.service_name != manifest.service_name
            ):
                raise ValueError("发布源事件、任务与服务不匹配")
            assessment = ReleaseAssessment(
                task_id=task.id,
                phase_version=task.status_version,
                manifest=manifest,
                manifest_hash=manifest_hash(manifest),
                purpose=request.purpose,
                checks=self.checks(manifest, records, request.purpose),
                canary_percent=self.settings.release_config.canary_percent,
                config_hash=configuration_hash(self.settings),
            )
            record = await ledger.append_evidence(
                task_id=task.id,
                source_tool="release.assessment",
                parameters=key,
                result_snapshot=json_object(assessment.model_dump(mode="json")),
            )
            return str(record.id)

    async def review(self, request: ReleasePlanRequest) -> str:
        key = json_object(asdict(request))
        async with self.database.session() as session, session.begin():
            task = await lock_task(session, request.task)
            ledger = LedgerService(session)
            previous = await cached(session, task.id, "release.review", key)
            if previous:
                return str(previous.id)
            record = await ledger.get_evidence(UUID(request.assessment_evidence_id))
            assessment = ReleaseAssessment.model_validate_json(json.dumps(record.result_snapshot))
            if (
                record.task_id != task.id
                or record.source_tool != "release.assessment"
                or assessment.phase_version != task.status_version
                or not assessment.passed
            ):
                raise ValueError("发布预检查未通过或已过期")
            manifest, facts = await self.facts(
                session, task.id, assessment.manifest.release_id, "codex-release-reviewer"
            )
            clear = manifest_hash(manifest) == assessment.manifest_hash and all(
                c.passed for c in self.checks(manifest, facts, assessment.purpose)
            )
            data = {
                "task_id": str(task.id),
                "phase_version": task.status_version,
                "assessment_evidence_id": str(record.id),
                "clear": clear,
                "evidence_ids": [str(e.id) for e in facts],
            }
            llm = FakeLLM([ScriptedChatStep(review_response)])
            try:
                response = await llm.chat(
                    ChatRequest(
                        messages=(
                            ChatMessage(
                                role="system",
                                content="尝试反证发布检查，分别复核发布、网络/第三方、缓存/资源及可观测性。",
                            ),
                            ChatMessage(role="user", content=json.dumps(data)),
                        ),
                    )
                )
            finally:
                await llm.aclose()
            if (
                response.finish_reason != "stop"
                or response.message.refusal
                or response.message.tool_calls
            ):
                raise ValueError("发布复核模型响应不完整")
            result = ReleaseReview.model_validate_json(response.message.content or "{}")
            if (
                not result.clear
                or not clear
                or result.task_id != task.id
                or result.phase_version != task.status_version
                or result.assessment_evidence_id != record.id
                or result.confidence != 0.8
                or result.evidence_ids != tuple(e.id for e in facts)
                or len(result.alternatives) != 4
                or any(
                    not set(c.evidence_ids) <= set(result.evidence_ids) for c in result.alternatives
                )
            ):
                raise ValueError("发布 Reviewer 发现反证或引用无效")
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="release.review",
                parameters=key,
                result_snapshot=json_object(result.model_dump(mode="json")),
            )
            return str(evidence.id)

    async def plan(self, request: ReleasePlanRequest) -> PlanningResult:
        async with self.database.session() as session, session.begin():
            task = await lock_task(session, request.task)
            await require_release_review(session, task)
            ledger = LedgerService(session)
            assessment_record = await ledger.get_evidence(UUID(request.assessment_evidence_id))
            assessment = ReleaseAssessment.model_validate_json(
                json.dumps(assessment_record.result_snapshot)
            )
            if (
                not request.review_evidence_id
                or assessment.phase_version != task.status_version - 1
                or task.status is not TaskStatus.PLANNING
            ):
                raise ValueError("发布计划缺少紧邻 RCA 的复核")
            key: JsonObject = {
                "phase_version": task.status_version,
                "conclusion_evidence_id": request.assessment_evidence_id,
                "review_evidence_id": request.review_evidence_id,
            }
            previous = await cached(session, task.id, "action_plan", key)
            if previous:
                return PlanningResult(
                    str(previous.id),
                    ActionPlan.model_validate_json(
                        json.dumps(previous.result_snapshot)
                    ).model_dump_json(),
                )
            manifest = assessment.manifest
            names = {
                "canary": "deploy_service",
                "promote": "deploy_service",
                "pause": "pause_release",
                "rollback": "rollback_prod",
            }
            params: JsonObject = (
                {"paused": True}
                if assessment.purpose == "pause"
                else {"from_version": manifest.to_version, "to_version": manifest.from_version}
                if assessment.purpose == "rollback"
                else {
                    "from_version": manifest.from_version
                    if assessment.purpose == "canary"
                    else manifest.to_version,
                    "to_version": manifest.to_version,
                    "traffic_percent": assessment.canary_percent
                    if assessment.purpose == "canary"
                    else 100,
                }
            )
            claim = EvidenceClaim(
                statement=f"阶段 {assessment.purpose} 检查及复核通过，等待 Policy 授权。",
                evidence_ids=(assessment_record.id, UUID(request.review_evidence_id)),
            )
            action = PlannedAction(
                id="release-" + assessment.purpose,
                name=names[assessment.purpose],
                service_name=manifest.service_name,
                parameters=params,
                risk_level=RiskLevel.L3,
                rationale=claim,
                preconditions=("发布材料、宿主绑定、当前版本和阶段仍与批准计划一致。",),
                rollback=RollbackPlan(
                    description="异常停止推广，并生成独立审批的回滚计划。",
                    parameters={
                        "from_version": manifest.to_version,
                        "to_version": manifest.from_version,
                    },
                    trigger="窗口指标异常或独立验证失败",
                ),
                verification=VerificationPlan(
                    checks=("Deployment/Pod", "5xx/P99/成功率", "日志/Trace/资源"),
                    success_criteria="目标版本、流量与全部独立窗口检查通过",
                    failure_response="暂停、重新调查或熔断转人工；不复用旧审批",
                ),
            )
            policy = create_policy_engine(self.settings)
            runbook = await task_runbook_context(
                session, task.id, self.settings.runbook_maturity_config
            )
            plan = ActionPlan(
                task_id=task.id,
                planning_version=task.status_version,
                conclusion_evidence_id=assessment_record.id,
                review_evidence_id=UUID(request.review_evidence_id),
                environment=policy.environment,
                summary=claim,
                actions=(
                    EvaluatedAction(
                        action=action,
                        policy=policy.evaluate(
                            PolicyAction(
                                name=action.name, risk_level=action.risk_level, runbook=runbook
                            )
                        ),
                    ),
                ),
                runbook=runbook,
            )
            await require_release_plan(session, task, plan, self.settings)
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="action_plan",
                parameters=key,
                result_snapshot=json_object(plan.model_dump(mode="json")),
            )
            return PlanningResult(str(evidence.id), plan.model_dump_json())


async def require_release_review(session: AsyncSession, task: AITask) -> bool:
    records = await LedgerService(session).evidence_for_task(task.id)
    assessments = [e for e in records if e.source_tool == "release.assessment"]
    if not assessments:
        return False
    latest = await session.scalar(
        select(TaskStatusHistory)
        .where(
            TaskStatusHistory.task_id == task.id,
            TaskStatusHistory.to_status.in_([TaskStatus.RCA, TaskStatus.INVESTIGATING]),
        )
        .order_by(TaskStatusHistory.sequence.desc())
        .limit(1)
    )
    if latest is None or latest.to_status is not TaskStatus.RCA:
        raise ValueError("发布必须完成当前 RCA 的复核")
    current = [
        e
        for e in assessments
        if isinstance((phase := e.parameters.get("task")), dict)
        and phase.get("version") == latest.sequence
    ]
    if len(current) != 1:
        raise ValueError("缺少唯一当前发布检查")
    assessment = ReleaseAssessment.model_validate_json(json.dumps(current[0].result_snapshot))
    reviews = [
        e
        for e in records
        if e.source_tool == "release.review"
        and e.parameters.get("assessment_evidence_id") == str(current[0].id)
    ]
    if len(reviews) != 1 or not assessment.passed:
        raise ValueError("发布没有通过检查和独立 Reviewer")
    review = ReleaseReview.model_validate_json(json.dumps(reviews[0].result_snapshot))
    if (
        not review.clear
        or review.task_id != task.id
        or review.phase_version != latest.sequence
        or review.assessment_evidence_id != current[0].id
        or len(review.alternatives) != 4
    ):
        raise ValueError("发布复核未通过或版本不匹配")
    audits = await LedgerService(session).audits_for_task(task.id)
    for refs, actor in (
        (tuple(r for c in assessment.checks for r in c.claim.evidence_ids), "codex-main-agent"),
        (review.evidence_ids, "codex-release-reviewer"),
    ):
        for reference in refs:
            fact = await LedgerService(session).get_evidence(reference)
            if fact.task_id != task.id or not any(
                a.evidence_id == reference
                and a.actor == actor
                and a.operation == fact.source_tool
                and a.outcome == "succeeded"
                and a.details.get("mode") == "live"
                for a in audits
            ):
                raise ValueError("发布结论或复核缺少成功事实审计")
    return True


async def require_release_plan(
    session: AsyncSession, task: AITask, plan: ActionPlan, settings: Settings
) -> None:
    if (
        not settings.release_config.enabled
        or settings.connector_mode.value != "fake"
        or settings.app_env not in {"local", "test"}
    ):
        raise PermissionError("发布动作尚未开放真实执行")
    if not await require_release_review(session, task) or len(plan.actions) != 1:
        raise ValueError("发布动作必须有当前检查和复核")
    record = await LedgerService(session).get_evidence(plan.conclusion_evidence_id)
    assessment = ReleaseAssessment.model_validate_json(json.dumps(record.result_snapshot))
    if (
        record.task_id != task.id
        or record.source_tool != "release.assessment"
        or not assessment.passed
        or assessment.phase_version != plan.planning_version - 1
        or assessment.config_hash != configuration_hash(settings)
        or manifest_hash(assessment.manifest) != assessment.manifest_hash
    ):
        raise ValueError("发布检查或宿主规则发生变化")
    review = await LedgerService(session).get_evidence(plan.review_evidence_id)
    if (
        review.task_id != task.id
        or review.source_tool != "release.review"
        or review.parameters.get("assessment_evidence_id") != str(record.id)
    ):
        raise ValueError("发布计划复核不匹配")
    action = plan.actions[0].action
    manifest = assessment.manifest
    records = await LedgerService(session).evidence_for_task(task.id)
    if assessment.purpose != "canary":
        from app.verifier.releases import ReleaseVerification

        proofs = [
            e
            for e in records
            if e.source_tool == "verify_release"
            and e.parameters.get("version") == assessment.phase_version - 2
        ]
        if len(proofs) != 1:
            raise PermissionError("发布阶段缺少紧邻的独立观测证据")
        proof = ReleaseVerification.model_validate_json(json.dumps(proofs[0].result_snapshot))
        prior_execution = await LedgerService(session).get_evidence(proof.execution_evidence_id)
        prior_observation = await LedgerService(session).get_evidence(proof.observation_evidence_id)
        observed = ReleaseObservation.model_validate_json(
            json.dumps(prior_observation.result_snapshot)
        )
        if (
            proof.task_id != task.id
            or prior_execution.task_id != task.id
            or prior_observation.task_id != task.id
            or observed.release_id != manifest.release_id
        ):
            raise PermissionError("前序发布观测不属于同一任务和发布申请")
        if assessment.purpose == "promote" and (
            not proof.passed
            or prior_execution.parameters.get("name") != "deploy_service"
            or observed.target.paused
            or observed.target.traffic_percent != assessment.canary_percent
        ):
            raise PermissionError("只有灰度独立验证通过才允许全量推广")
        if assessment.purpose == "pause" and (
            proof.passed or prior_execution.parameters.get("name") != "deploy_service"
        ):
            raise PermissionError("暂停必须绑定前序发布窗口异常")
        if assessment.purpose == "rollback" and (
            not proof.target_matches
            or prior_execution.parameters.get("name") != "pause_release"
            or not observed.target.paused
        ):
            raise PermissionError("回滚前必须独立读回暂停结果")
    expected: JsonObject = (
        {"paused": True}
        if assessment.purpose == "pause"
        else {"from_version": manifest.to_version, "to_version": manifest.from_version}
        if assessment.purpose == "rollback"
        else {
            "from_version": manifest.from_version
            if assessment.purpose == "canary"
            else manifest.to_version,
            "to_version": manifest.to_version,
            "traffic_percent": assessment.canary_percent if assessment.purpose == "canary" else 100,
        }
    )
    name = {
        "canary": "deploy_service",
        "promote": "deploy_service",
        "pause": "pause_release",
        "rollback": "rollback_prod",
    }[assessment.purpose]
    if (action.name, action.parameters, action.service_name, action.risk_level) != (
        name,
        expected,
        manifest.service_name,
        RiskLevel.L3,
    ):
        raise PermissionError("发布计划改变了已检查的阶段、服务、参数或风险")
