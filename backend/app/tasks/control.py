"""人工操作经持久化 Temporal 命令提交，复用既有审批和问答事务。"""

import asyncio
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.config import Settings
from app.connectors.feishu.factory import create_feishu_connector
from app.db.session import Database
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.tasks.approval.service import ApprovalStore
from app.tasks.console_models import (
    AnswerInput,
    ApprovalInput,
    ControlCommand,
    ControlReceipt,
    TakeoverInput,
)
from app.tasks.console_queries import ConsoleConflict, ConsoleNotFound, ConsoleQueries
from app.tasks.human.activities import HumanInteractionStore
from app.tasks.human.models import question_id
from app.tasks.models import AITask
from app.tasks.service import TaskService
from app.tasks.states import ALLOWED_TRANSITIONS, TaskStatus
from app.tasks.workflow_models import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    HumanAnswer,
    HumanAnswerRequest,
    HumanResponse,
    WorkflowProgress,
)
from app.tools.registry import json_object


def operation_id(command: ControlCommand) -> str:
    digest = hashlib.sha256(
        json.dumps(
            command.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return f"task-control-{command.task_id}-{digest}"


class ControlStore:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    async def record(
        self, command: ControlCommand, *, allow_recovery: bool = False
    ) -> ControlReceipt:
        identity = operation_id(command)
        if command.kind == "takeover":
            return await self.takeover(command)
        async with self.database.session() as session:
            queries = ConsoleQueries(session)
            if command.kind == "approval":
                body = ApprovalInput.model_validate(command.payload)
                ticket, prompt = await queries.approval(command.task_id, body.wait_version)
                if (body.approval_id, body.action_hash) != (ticket.approval_id, ticket.action_hash):
                    raise ConsoleConflict("审批单或动作哈希不匹配")
                response = ApprovalResponse(
                    str(command.task_id),
                    str(body.approval_id),
                    body.wait_version,
                    body.action_hash,
                    body.decision,
                    command.actor,
                )
                request = ApprovalDecisionRequest(prompt, response)
            else:
                answer = AnswerInput.model_validate(command.payload)
                expected = (
                    TaskStatus.NEED_HUMAN_JUDGMENT
                    if command.kind == "judgment"
                    else TaskStatus.WAITING_INFORMATION
                )
                try:
                    human = await queries.question(command.task_id, answer.wait_version)
                except ConsoleNotFound:
                    recovery = (
                        await queries.recovery(command.task_id, answer.wait_version)
                        if allow_recovery
                        else None
                    )
                    if recovery is None:
                        raise
                    if (
                        UUID(question_id(recovery.task)) != answer.question_id
                        or recovery.task.status is not expected
                    ):
                        raise ConsoleConflict("恢复回答与当前等待身份不符") from None
                    # 旧巡检/占位等待只有恢复信号，先用既有问答服务留存真实问题。
                    connector = create_feishu_connector(self.settings)
                    try:
                        human = await HumanInteractionStore(self.database).notify(
                            recovery, connector
                        )
                    finally:
                        await connector.aclose()
                if (
                    UUID(human.question_id) != answer.question_id
                    or human.task.status is not expected
                ):
                    raise ConsoleConflict("回答不属于当前判断或补充信息问题")
        if command.kind == "approval":
            result = await ApprovalStore(self.database, self.settings).decide(request)
            evidence_id = UUID(result.evidence_id)
        else:
            recorded = await HumanInteractionStore(self.database).answer(
                HumanAnswerRequest(
                    human,
                    HumanAnswer(
                        human.question_id,
                        human.task.status,
                        human.task.version,
                        answer.answer,
                        command.actor,
                    ),
                )
            )
            evidence_id = UUID(recorded.answer_evidence_id)
        return ControlReceipt(
            task_id=command.task_id,
            operation_id=identity,
            evidence_id=evidence_id,
            outcome="signaled",
        )

    async def takeover(self, command: ControlCommand) -> ControlReceipt:
        body = TakeoverInput.model_validate(command.payload)
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == command.task_id).with_for_update()
            )
            if task is None:
                raise ConsoleNotFound("任务不存在")
            previous = await session.scalar(
                select(Evidence).where(
                    Evidence.task_id == task.id, Evidence.source_tool == "human.takeover"
                )
            )
            payload = command.model_dump(mode="json")
            if previous is not None:
                if previous.parameters != payload:
                    raise ConsoleConflict("任务已被其他接管请求停止")
                return ControlReceipt(
                    task_id=task.id,
                    operation_id=operation_id(command),
                    evidence_id=previous.id,
                    outcome="taken_over",
                )
            if task.status_version != body.expected_version:
                raise ConsoleConflict("任务版本已变化，请刷新后接管")
            if (
                task.status is not TaskStatus.ESCALATED
                and TaskStatus.ESCALATED not in ALLOWED_TRANSITIONS[task.status]
            ):
                raise ConsoleConflict("当前任务不能接管")
            ledger = LedgerService(session)
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="human.takeover",
                parameters=json_object(payload),
                result_snapshot={
                    "actor": command.actor,
                    "reason": body.reason,
                    "previous_status": task.status.value,
                    "previous_version": task.status_version,
                },
            )
            if task.status is not TaskStatus.ESCALATED:
                await TaskService(session).transition(
                    task.id,
                    TaskStatus.ESCALATED,
                    expected_status=task.status,
                    expected_version=task.status_version,
                    reason=f"人工接管：{body.reason}；证据 {evidence.id}",
                )
            await ledger.append_audit(
                task_id=task.id,
                event_type=AuditEventType.HUMAN_INTERACTION,
                actor=command.actor,
                operation="human.takeover",
                outcome="taken_over",
                evidence_id=evidence.id,
                details={"reason": body.reason},
            )
            return ControlReceipt(
                task_id=task.id,
                operation_id=operation_id(command),
                evidence_id=evidence.id,
                outcome="taken_over",
            )


class ControlActivities:
    def __init__(self, database: Database, settings: Settings, client: Client) -> None:
        self.store, self.client = ControlStore(database, settings), client

    @activity.defn(name="task.control.record")
    async def record(self, command_json: str) -> str:
        try:
            command = ControlCommand.model_validate_json(command_json)
            allow_recovery = False
            if command.kind in {"judgment", "information"}:
                body = AnswerInput.model_validate(command.payload)
                async with self.store.database.session() as session:
                    recovery = await ConsoleQueries(session).recovery(
                        command.task_id, body.wait_version
                    )
                if recovery is not None:
                    try:
                        progress = await self.client.get_workflow_handle(
                            f"ai-task-{command.task_id}"
                        ).query("progress", result_type=WorkflowProgress)
                        allow_recovery = progress.legacy_wait
                    except RPCError as error:
                        if error.status is RPCStatusCode.NOT_FOUND:
                            raise ConsoleConflict("目标 Workflow 不存在或已结束") from None
                        raise
            receipt = await self.store.record(command, allow_recovery=allow_recovery)
            return receipt.model_dump_json()
        except ConsoleNotFound:
            raise ApplicationError(
                "记录不存在", type="ConsoleNotFound", non_retryable=True
            ) from None
        except (ValueError, LookupError):
            raise ApplicationError(
                "人工操作已过期或与已记录决定冲突", type="ConsoleConflict", non_retryable=True
            ) from None

    @activity.defn(name="task.control.deliver")
    async def deliver(self, command_json: str, receipt_json: str) -> None:
        command = ControlCommand.model_validate_json(command_json)
        receipt = ControlReceipt.model_validate_json(receipt_json)
        handle = self.client.get_workflow_handle(f"ai-task-{command.task_id}")
        try:
            if command.kind == "takeover":
                await handle.cancel(reason="人工已接管；持久化门禁阻止后续自动化")
                return
            async with self.store.database.session() as session:
                queries = ConsoleQueries(session)
                progress = await handle.query("progress", result_type=WorkflowProgress)
                if command.kind == "approval":
                    body = ApprovalInput.model_validate(command.payload)
                    _, prompt = await queries.approval(command.task_id, body.wait_version)
                    if (
                        progress.approval_result is not None
                        and progress.approval_result.evidence_id == str(receipt.evidence_id)
                    ):
                        return
                    if progress.task != prompt.task:
                        raise ApplicationError(
                            "审批等待已结束", type="ConsoleConflict", non_retryable=True
                        )
                    if progress.approval_prompt != prompt:
                        raise ApplicationError("等待 Workflow 加载已提交审批单")
                    await handle.signal(
                        "approve_actions",
                        ApprovalResponse(
                            str(command.task_id),
                            prompt.approval_id,
                            prompt.task.version,
                            prompt.action_hash,
                            body.decision,
                            command.actor,
                        ),
                    )
                else:
                    answer = AnswerInput.model_validate(command.payload)
                    prompt_human = await queries.question(command.task_id, answer.wait_version)
                    if any(
                        item.answer_evidence_id == str(receipt.evidence_id)
                        for item in progress.human_answers
                    ):
                        return
                    if progress.human_prompt is None:
                        history = await queries.history(command.task_id)
                        if any(
                            row.sequence == prompt_human.task.version + 1
                            and row.from_status is prompt_human.task.status
                            and row.to_status is prompt_human.resume_status
                            for row in history
                        ):
                            return
                    if progress.task != prompt_human.task:
                        raise ApplicationError(
                            "人工等待已结束", type="ConsoleConflict", non_retryable=True
                        )
                    if progress.legacy_wait:
                        await handle.signal(
                            "human_response",
                            HumanResponse(
                                prompt_human.task.status, prompt_human.task.version, True
                            ),
                        )
                        return
                    if progress.human_prompt != prompt_human:
                        raise ApplicationError("等待 Workflow 加载已提交问题")
                    await handle.signal(
                        "answer_question",
                        HumanAnswer(
                            prompt_human.question_id,
                            prompt_human.task.status,
                            prompt_human.task.version,
                            answer.answer,
                            command.actor,
                        ),
                    )
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                if command.kind == "takeover":
                    return  # 已关闭/未派发任务的停止记录同样有效。
                raise ApplicationError(
                    "目标 Workflow 已结束或不存在", type="ConsoleConflict", non_retryable=True
                ) from None
            raise


class ControlGateway:
    def __init__(self, client: Client, settings: Settings, database: Database) -> None:
        self.client, self.settings, self.database = client, settings, database

    async def submit(self, command: ControlCommand) -> ControlReceipt:
        from app.tasks.control_workflow import TaskControlWorkflow

        async with self.database.session() as session:
            await ConsoleQueries(session).task(command.task_id)
        identity = operation_id(command)
        try:
            handle = await self.client.start_workflow(
                TaskControlWorkflow.run,
                command.model_dump_json(),
                id=identity,
                task_queue=self.settings.temporal_config.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(identity, result_type=str)
        try:
            result = await asyncio.wait_for(
                handle.result(), self.settings.trigger_config.response_timeout_seconds
            )
        except WorkflowFailureError as error:
            cause: BaseException | None = error
            while cause is not None:
                if isinstance(cause, ApplicationError) and cause.type == "ConsoleNotFound":
                    raise ConsoleNotFound("记录不存在") from None
                if isinstance(cause, ApplicationError) and cause.type == "ConsoleConflict":
                    raise ConsoleConflict("操作已过期、Workflow 已结束或与已有决定冲突") from None
                cause = cause.__cause__
            raise
        return ControlReceipt.model_validate_json(result)
