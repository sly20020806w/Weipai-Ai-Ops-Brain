"""输入和报告只追加留证；统一事件/任务入口，行锁保证并发与重投幂等。"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.client import LLMClient
from app.db.base import utc_now
from app.knowledge.schemas import KnowledgeSearch, KnowledgeType
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.runbooks.schemas import MatchingFacts, RunbookSearch, applicability
from app.tasks.architecture.engine import assess, validate_citations
from app.tasks.architecture.models import (
    ArchitectureReport,
    ArchitectureRequest,
    ArchitectureResult,
    ReviewSources,
    ReviewSubmission,
)
from app.tasks.models import AITask
from app.tasks.states import TaskSource, TaskStatus
from app.tools.architecture import ALLOWED_TOOLS
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus, JsonObject
from app.tools.registry import json_object
from app.tools.runbooks import SearchRunbooksOutput
from app.triggers.models import OpsEvent
from app.triggers.schemas import EventReceipt, NormalizedEvent
from app.triggers.service import EventService

ACTOR = "architecture-review"


async def submit_review(session: AsyncSession, value: ReviewSubmission) -> EventReceipt:
    value = ReviewSubmission.model_validate(value)
    receipt = (
        await EventService(session).accept(
            [
                NormalizedEvent(
                    origin="manual",
                    source=TaskSource.HUMAN,
                    external_id=f"architecture-review:{value.request_id}",
                    service_name=value.service_name,
                    title=f"架构评审：{value.title}",
                    occurred_at=utc_now(),
                )
            ]
        )
    )[0]
    # EventService 已持有去重锁；同一 request_id 不能换方案或服务。
    ledger = LedgerService(session)
    records = await ledger.evidence_for_task(UUID(receipt.task_id))
    saved = next((e for e in records if e.source_tool == "architecture.submission"), None)
    if saved is not None:
        if saved.result_snapshot != value.model_dump(mode="json"):
            raise ValueError("同一架构评审请求不能修改已提交的方案")
    else:
        await ledger.append_evidence(
            task_id=UUID(receipt.task_id),
            source_tool="architecture.submission",
            parameters={"request_id": str(value.request_id)},
            result_snapshot=value.model_dump(mode="json"),
        )
    return receipt


async def submission(session: AsyncSession, task: AITask) -> tuple[Evidence, ReviewSubmission]:
    event = await session.scalar(select(OpsEvent).where(OpsEvent.task_id == task.id))
    records = [
        e
        for e in await LedgerService(session).evidence_for_task(task.id)
        if e.source_tool == "architecture.submission"
    ]
    if len(records) != 1:
        raise ValueError("架构评审缺少唯一的方案输入快照")
    record = records[0]
    value = ReviewSubmission.model_validate_json(json.dumps(record.result_snapshot))
    if (
        task.source is not TaskSource.HUMAN
        or event is None
        or event.origin != "manual"
        or event.external_id != f"architecture-review:{value.request_id}"
        or event.service_name != value.service_name
        or task.title != f"架构评审：{value.title}"
        or record.parameters != {"request_id": str(value.request_id)}
    ):
        raise ValueError("方案与归一化事件/任务身份不匹配")
    return record, value


def result_for(record: Evidence) -> ArchitectureResult:
    if record.source_tool == "architecture.blocked":
        return ArchitectureResult(str(record.id), None, True)
    report = ArchitectureReport.model_validate_json(json.dumps(record.result_snapshot))
    return ArchitectureResult(str(record.id), report.model_dump_json())


class ArchitectureService:
    def __init__(self, session: AsyncSession, dispatcher: ToolDispatcher, llm: LLMClient) -> None:
        self.session, self.dispatcher, self.llm = session, dispatcher, llm

    async def collect(self, request: ArchitectureRequest) -> ArchitectureResult | None:
        if request.task.status is not TaskStatus.INVESTIGATING:
            raise ValueError("架构评审只接受 INVESTIGATING 阶段")
        task = await self.session.scalar(
            select(AITask)
            .where(AITask.id == UUID(request.task.task_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            raise ValueError("架构评审任务不存在")
        ledger = LedgerService(self.session)
        input_record, value = await submission(self.session, task)
        key: JsonObject = {"phase_version": request.task.version, "submission_hash": value.digest}
        saved = next(
            (
                e
                for e in await ledger.evidence_for_task(task.id)
                if e.source_tool in {"architecture.report", "architecture.blocked"}
                and e.parameters == key
            ),
            None,
        )
        if saved is not None:
            return result_for(saved)
        if task.status is not request.task.status or task.status_version != request.task.version:
            raise ValueError("架构评审阶段或版本已变化")
        if any(
            e.source_tool == "architecture.context" and e.parameters == key
            for e in await ledger.evidence_for_task(task.id)
        ):
            return None
        source_ids = {"proposal": str(input_record.id)}
        runbook_decisions: list[list[object]] = []
        queries = (
            (
                "runbooks",
                "search_runbooks",
                RunbookSearch(query=f"{value.service_name} 架构评审", limit=10).model_dump(
                    mode="json"
                ),
            ),
            ("context", "get_service_context", {"service_name": value.service_name}),
            (
                "standards",
                "search_knowledge",
                KnowledgeSearch(
                    query=f"{value.service_name} {value.proposal}"[:20000],
                    kind=KnowledgeType.STANDARD,
                    at=utc_now(),
                    limit=10,
                ).model_dump(mode="json"),
            ),
            (
                "incidents",
                "search_incidents",
                {"query": value.service_name, "service_name": value.service_name, "limit": 10},
            ),
        )
        for role, tool, parameters in queries:
            dispatch = await self.dispatcher.dispatch(
                task_id=task.id,
                tool_name=tool,
                parameters=json_object(parameters),
                actor=ACTOR,
                allowed_tools=ALLOWED_TOOLS,
            )
            if dispatch.status is not DispatchStatus.SUCCEEDED or dispatch.evidence_id is None:
                blocked = await ledger.append_evidence(
                    task_id=task.id,
                    source_tool="architecture.blocked",
                    parameters=key,
                    result_snapshot={
                        "tool": tool,
                        "status": dispatch.status.value,
                        "error_code": dispatch.error_code,
                    },
                )
                return result_for(blocked)  # 保留 Policy 拒绝/查询失败审计，父 Workflow 转人工。
            source_ids[role] = str(dispatch.evidence_id)
            if role == "runbooks":
                matches = SearchRunbooksOutput.model_validate_json(json.dumps(dispatch.result))
                runbook_decisions = [
                    list(
                        applicability(
                            hit.runbook,
                            MatchingFacts(
                                service_name=value.service_name,
                                title=task.title,
                                task_source=task.source.value,
                            ),
                        )
                    )
                    for hit in matches.matches
                ]
                await ledger.append_evidence(
                    task_id=task.id,
                    source_tool="architecture.runbook_matching",
                    parameters=key,
                    result_snapshot=json_object(
                        {
                            "search_evidence_id": str(dispatch.evidence_id),
                            "decisions": runbook_decisions,
                        }
                    ),
                )
        await ledger.append_evidence(
            task_id=task.id,
            source_tool="architecture.context",
            parameters=key,
            result_snapshot=json_object(
                {
                    "sources": source_ids,
                    "runbook_applicability": runbook_decisions,
                }
            ),
        )
        return None

    async def review(self, request: ArchitectureRequest) -> ArchitectureResult:
        task = await self.session.scalar(
            select(AITask)
            .where(AITask.id == UUID(request.task.task_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None or request.task.status is not TaskStatus.INVESTIGATING:
            raise ValueError("架构评审任务或请求阶段不匹配")
        ledger = LedgerService(self.session)
        _, value = await submission(self.session, task)
        key: JsonObject = {"phase_version": request.task.version, "submission_hash": value.digest}
        records = await ledger.evidence_for_task(task.id)
        saved = next(
            (e for e in records if e.source_tool == "architecture.report" and e.parameters == key),
            None,
        )
        if saved is not None:
            return result_for(saved)
        if task.status is not request.task.status or task.status_version != request.task.version:
            raise ValueError("架构评审阶段或版本已变化")
        contexts = [
            e for e in records if e.source_tool == "architecture.context" and e.parameters == key
        ]
        if len(contexts) != 1:
            raise ValueError("必须先提交唯一的评审查询检查点")
        context = json_object(contexts[0].result_snapshot)
        sources = ReviewSources.model_validate_json(json.dumps(context["sources"]))
        snapshots = {
            str(e.id): json_object(e.result_snapshot) for e in records if e.id in sources.ids
        }
        if len(snapshots) != 5:
            raise ValueError("评审来源快照缺失")
        draft = await assess(self.llm, json_object({**context, "snapshots": snapshots}))
        validate_citations(draft, {UUID(k): v for k, v in snapshots.items()})
        report = ArchitectureReport(
            dimensions=draft.dimensions,
            task_id=task.id,
            phase_version=task.status_version,
            submission_hash=value.digest,
            service_name=value.service_name,
            sources=sources,
            reviewed_at=utc_now(),
        )
        saved = await ledger.append_evidence(
            task_id=task.id,
            source_tool="architecture.report",
            parameters=key,
            result_snapshot=report.model_dump(mode="json"),
        )
        return result_for(saved)
