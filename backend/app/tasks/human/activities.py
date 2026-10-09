"""问答事务、飞书通知与 Knowledge 草稿；重试由 Temporal 负责。"""

from dataclasses import asdict
from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.feishu.base import FeishuConnector
from app.connectors.feishu.factory import create_feishu_connector
from app.connectors.feishu.models import CardButton, CardNotification, InteractiveCard
from app.db.session import Database
from app.knowledge.human_drafts import HumanKnowledgeDraft
from app.ledger.models import AuditEventType, Evidence
from app.ledger.service import LedgerService
from app.tasks.human.models import question_id, validate_answer, validate_question
from app.tasks.models import AITask
from app.tasks.states import TaskStatus, validate_transition
from app.tasks.workflow_models import (
    HumanAnswerRequest,
    HumanAnswerResult,
    HumanPrompt,
    HumanQuestion,
    HumanWaitRequest,
)
from app.tools.registry import json_object


def question_card(request: HumanWaitRequest) -> CardNotification:
    validate_question(HumanQuestion(request.task.status, request.question))
    if type(request.task.version) is not int or request.task.version < 1:
        raise ValueError("等待版本无效")
    if request.resume_status not in {
        TaskStatus.CONTEXT_BUILDING,
        TaskStatus.RUNBOOK_MATCHING,
        TaskStatus.INVESTIGATING,
        TaskStatus.RCA,
        TaskStatus.PLANNING,
    }:
        raise ValueError("人工回答只能恢复调查和规划")
    validate_transition(request.task.status, request.resume_status)
    title = (
        "需要你的业务判断"
        if request.task.status is TaskStatus.NEED_HUMAN_JUDGMENT
        else "需要补充信息"
    )
    identity = question_id(request.task)
    return CardNotification(
        notification_id=UUID(identity),
        card=InteractiveCard(
            title=title,
            markdown=f"任务：{request.task.task_id}\n状态：{request.task.status.value}\n\n{request.question}",
            buttons=(
                CardButton(
                    label="回答问题",
                    value={
                        "operation": "human_answer",
                        "question_id": identity,
                        "task_id": request.task.task_id,
                        "wait_status": request.task.status.value,
                        "wait_version": request.task.version,
                    },
                ),
            ),
        ),
    )


class HumanInteractionStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def notify(self, request: HumanWaitRequest, connector: FeishuConnector) -> HumanPrompt:
        card = question_card(request)
        parameters = json_object(asdict(request))
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == UUID(request.task.task_id)).with_for_update()
            )
            if task is None:
                raise ValueError("人工问答任务不存在")
            ledger = LedgerService(session)
            cached = await session.scalar(
                select(Evidence).where(
                    Evidence.task_id == task.id,
                    Evidence.source_tool == "human.question",
                    Evidence.parameters["task"]["version"].as_integer() == request.task.version,
                )
            )
            if cached is not None:
                if cached.parameters != parameters:
                    raise ValueError("同一等待版本不能改写问题")
                return HumanPrompt(
                    request.task,
                    str(card.notification_id),
                    str(cached.id),
                    request.question,
                    request.resume_status,
                )
            if (task.status, task.status_version) != (request.task.status, request.task.version):
                raise ValueError("问题对应的等待状态已过期")
            # 固定 notification_id 交由 Connector 的幂等协议去重，不能使用新随机 ID 重发。
            receipt = await connector.send(card)
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="human.question",
                parameters=parameters,
                result_snapshot={
                    "notification": json_object(card.model_dump(mode="json")),
                    "receipt": json_object(receipt.model_dump(mode="json")),
                },
            )
            await ledger.append_audit(
                task_id=task.id,
                event_type=AuditEventType.HUMAN_INTERACTION,
                actor="workflow",
                operation="human.question",
                outcome="sent",
                evidence_id=evidence.id,
                details={"question_id": str(card.notification_id)},
            )
            return HumanPrompt(
                request.task,
                str(card.notification_id),
                str(evidence.id),
                request.question,
                request.resume_status,
            )

    async def answer(self, request: HumanAnswerRequest) -> HumanAnswerResult:
        prompt, response = request.prompt, request.response
        validate_answer(response)
        if (response.question_id, response.wait_status, response.wait_version) != (
            question_id(prompt.task),
            prompt.task.status,
            prompt.task.version,
        ) or prompt.question_id != response.question_id:
            raise ValueError("回答与任务等待身份不符")
        question_card(HumanWaitRequest(prompt.task, prompt.question, prompt.resume_status))
        async with self.database.session() as session, session.begin():
            task = await session.scalar(
                select(AITask).where(AITask.id == UUID(prompt.task.task_id)).with_for_update()
            )
            if task is None:
                raise ValueError("人工问答任务不存在")
            ledger = LedgerService(session)
            question = await ledger.get_evidence(UUID(prompt.question_evidence_id))
            if (
                question.task_id != task.id
                or question.source_tool != "human.question"
                or question.parameters
                != json_object(
                    asdict(HumanWaitRequest(prompt.task, prompt.question, prompt.resume_status))
                )
            ):
                raise ValueError("问题证据不是本任务的当前问题")
            cached = await session.scalar(
                select(HumanKnowledgeDraft).where(
                    HumanKnowledgeDraft.task_id == task.id,
                    HumanKnowledgeDraft.wait_version == prompt.task.version,
                )
            )
            payload = json_object(asdict(request))
            if cached is not None:
                previous = await ledger.get_evidence(cached.answer_evidence_id)
                if previous.parameters != payload:
                    raise ValueError("同一问题已收到其他回答")
                return HumanAnswerResult(str(previous.id), str(cached.id))
            if (task.status, task.status_version) != (prompt.task.status, prompt.task.version):
                raise ValueError("回答对应的等待状态已过期")
            evidence = await ledger.append_evidence(
                task_id=task.id,
                source_tool="human.answer",
                parameters=payload,
                result_snapshot={
                    "question": prompt.question,
                    "answer": response.answer,
                    "respondent": response.respondent,
                },
                source_reference=f"ai-task:{task.id}/question:{prompt.question_id}",
            )
            draft = HumanKnowledgeDraft(
                task_id=task.id,
                wait_version=prompt.task.version,
                wait_status=prompt.task.status.value,
                question=prompt.question,
                answer=response.answer,
                respondent=response.respondent,
                answer_evidence_id=evidence.id,
            )
            session.add(draft)
            await session.flush()
            await ledger.append_audit(
                task_id=task.id,
                event_type=AuditEventType.HUMAN_INTERACTION,
                actor=response.respondent,
                operation="human.answer",
                outcome="recorded",
                evidence_id=evidence.id,
                details={"question_id": prompt.question_id, "knowledge_draft_id": str(draft.id)},
            )
            return HumanAnswerResult(str(evidence.id), str(draft.id))


class HumanActivities:
    def __init__(
        self, database: Database, settings: Settings, *, connector: FeishuConnector | None = None
    ) -> None:
        self.store, self.settings, self.connector = (
            HumanInteractionStore(database),
            settings,
            connector,
        )

    @activity.defn(name="human.notify")
    async def notify(self, request: HumanWaitRequest) -> HumanPrompt:
        connector = self.connector or create_feishu_connector(self.settings)
        try:
            return await self.store.notify(request, connector)
        except (ValueError, LookupError):
            raise ApplicationError("人工问题被拒绝", non_retryable=True) from None
        except Exception:
            raise ApplicationError("人工问题通知失败") from None
        finally:
            if self.connector is None:
                await connector.aclose()

    @activity.defn(name="human.record_answer")
    async def record_answer(self, request: HumanAnswerRequest) -> HumanAnswerResult:
        try:
            return await self.store.answer(request)
        except (ValueError, LookupError):
            raise ApplicationError("人工回答被拒绝", non_retryable=True) from None
        except Exception:
            raise ApplicationError("人工回答保存失败") from None
