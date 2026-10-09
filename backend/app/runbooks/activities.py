"""先经 Dispatcher 检索，再依据任务事实判定条件，结果追加到 Ledger。"""

import json

from pydantic import TypeAdapter
from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.activities import lock_task
from app.agent.investigation import InvalidConclusion, InvestigationSpec
from app.config import Settings
from app.db.session import Database
from app.ledger.models import Evidence
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.runbooks.schemas import MatchingFacts, applicability
from app.runbooks.workflow_models import RunbookMatchRequest, RunbookMatchResult, spec_hash
from app.tasks.states import TaskStatus
from app.tools.dispatcher import ToolDispatcher
from app.tools.models import DispatchStatus
from app.tools.registry import json_object
from app.tools.runbooks import SearchRunbooksOutput
from app.tools.runtime import investigation_registry


class RunbookActivities:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    @activity.defn(name="runbook.match")
    async def match(self, request: RunbookMatchRequest) -> RunbookMatchResult:
        try:
            spec = InvestigationSpec.model_validate_json(request.spec_json)
            if request.task.status is not TaskStatus.RUNBOOK_MATCHING:
                raise ValueError("只有 RUNBOOK_MATCHING 阶段可匹配 Runbook")
            key = json_object(
                {"phase_version": request.task.version, "spec_hash": spec_hash(request.spec_json)}
            )
            async with self.database.session() as session, session.begin():
                task = await lock_task(session, request.task)
                ledger = LedgerService(session)
                cached = await session.scalar(
                    select(Evidence).where(
                        Evidence.task_id == task.id,
                        Evidence.source_tool == "runbook.match",
                        Evidence.parameters["phase_version"].as_integer() == request.task.version,
                    )
                )
                if cached is not None:
                    if cached.parameters != key:
                        raise ValueError("Runbook 匹配检查点请求冲突")
                    snapshot = cached.result_snapshot
                    assert snapshot is not None
                    return TypeAdapter(RunbookMatchResult).validate_json(json.dumps(snapshot))
                async with investigation_registry(self.settings, session) as registry:
                    result = await ToolDispatcher(
                        registry, create_policy_engine(self.settings), ledger
                    ).dispatch(
                        task_id=task.id,
                        tool_name="search_runbooks",
                        parameters={"query": f"{spec.service_name} {spec.title}", "limit": 100},
                        actor="codex-main-agent",
                    )
                blocked = (
                    result.status is not DispatchStatus.SUCCEEDED or result.evidence_id is None
                )
                hits = (
                    SearchRunbooksOutput(matches=())
                    if blocked
                    else SearchRunbooksOutput.model_validate_json(json.dumps(result.result))
                )
                facts = MatchingFacts(
                    service_name=spec.service_name, title=spec.title, task_source=task.source.value
                )
                selected = None
                reasons = []
                for hit in hits.matches:
                    allowed, reason = applicability(hit.runbook, facts)
                    reasons.append(reason)
                    if allowed:
                        selected = hit.runbook
                        break
                matched = RunbookMatchResult(
                    selected.model_dump_json() if selected is not None else None,
                    reasons[-1]
                    if selected is not None
                    else ("；".join(dict.fromkeys(reasons)) or "未检索到 Runbook，转自主调查"),
                    str(result.evidence_id) if result.evidence_id is not None else None,
                    blocked,
                )
                if blocked:
                    matched = RunbookMatchResult(
                        None, "Runbook 检索被拒绝或失败，转交人工", None, True
                    )
                await ledger.append_evidence(
                    task_id=task.id,
                    source_tool="runbook.match",
                    parameters=key,
                    result_snapshot=json_object(
                        {
                            "runbook_json": matched.runbook_json,
                            "reason": matched.reason,
                            "search_evidence_id": matched.search_evidence_id,
                            "blocked": matched.blocked,
                        }
                    ),
                )
                return matched
        except (ValueError, InvalidConclusion):
            raise ApplicationError("Runbook 匹配输入或结果被拒绝", non_retryable=True) from None
        except Exception:
            raise ApplicationError("Runbook 匹配失败") from None
