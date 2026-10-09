"""确定性 Temporal 编排；数据库与阶段工作全部在 Activity 中完成。"""

import json
import math
from datetime import timedelta
from typing import cast
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.agent.chat.models import ChatVerifyRequest
    from app.agent.investigation import InvestigationResult, InvestigationSpec
    from app.agent.reviewer.models import ReviewDecision, ReviewRequest, ReviewResult
    from app.agent.workflow_models import ConclusionRequest, InvestigationRequest
    from app.executor.models import ExecutionRequest, ExecutionResult
    from app.learning.models import LearningRequest, LearningResult
    from app.policy.models import PolicyDecision
    from app.runbooks.workflow_models import RunbookMatchRequest, RunbookMatchResult
    from app.tasks.approval.models import validate_response
    from app.tasks.architecture.models import (
        ArchitectureRequest,
        ArchitectureResult,
        ArchitectureVerifyRequest,
    )
    from app.tasks.human.models import validate_answer, validate_question
    from app.tasks.inspection.catalog import Mode as InspectionMode
    from app.tasks.inspection.models import (
        InspectionReport,
        InspectionRequest,
        InspectionVerifyRequest,
    )
    from app.tasks.inspection.workflow import InspectionWorkflow
    from app.tasks.planning.models import ActionPlan, PlanningRequest, PlanningResult
    from app.tasks.releases.models import (
        Purpose,
        ReleaseAssessment,
        ReleaseObserveRequest,
        ReleaseObserveResult,
        ReleasePlanRequest,
        ReleaseStageRequest,
    )
    from app.tasks.safety.models import SafetyResult
    from app.tasks.states import TaskStatus
    from app.tasks.tickets.models import (
        TicketAnalysisRequest,
        TicketCategory,
        TicketContext,
        TicketExecutionRequest,
        TicketPlanRequest,
        TicketStageRequest,
        TicketVerifyRequest,
        TicketVerifyResult,
    )
    from app.tasks.war_room.models import (
        WarRoomAssessment,
        WarRoomRequest,
        WarRoomResult,
        WarRoomSubmission,
        WarRoomVerified,
        WarRoomVerifyRequest,
    )
    from app.tasks.workflow_models import (
        ApprovalDecisionRequest,
        ApprovalPrompt,
        ApprovalRequest,
        ApprovalResponse,
        ApprovalResult,
        HumanAnswer,
        HumanAnswerRequest,
        HumanAnswerResult,
        HumanPrompt,
        HumanQuestion,
        HumanResponse,
        HumanWaitRequest,
        TaskSnapshot,
        TransitionRequest,
        WorkflowInput,
        WorkflowProgress,
    )
    from app.verifier.models import VerificationRequest, VerificationResult, VerificationSpec


WAITING_STATUSES = frozenset(
    {TaskStatus.WAITING_INFORMATION, TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_APPROVAL}
)


def validate_workflow_input(value: WorkflowInput) -> None:
    if value.chat_mode is not None and (
        value.chat_mode not in {"question", "task"}
        or value.investigation_json is None
        or value.waits
        or value.human_questions
        or value.verification_json
        or value.war_room
        or value.architecture_review
        or value.inspection_mode
        or value.ticket_id
        or value.release_id
        or (value.chat_mode == "question" and value.execution_enabled)
    ):
        raise ValueError("对话必须使用主 Agent 调查，且只读模式不能启用执行")
    if type(value.war_room) is not bool:
        raise ValueError("重大保障入口必须是宿主布尔标记")
    if value.war_room and (
        value.architecture_review
        or value.inspection_mode is not None
        or value.ticket_id is not None
        or value.release_id is not None
        or value.waits
        or value.investigation_json
        or value.verification_json
        or value.human_questions
    ):
        raise ValueError("重大保障不能混用其他任务场景")
    if type(value.architecture_review) is not bool:
        raise ValueError("架构评审入口必须使用宿主布尔标记")
    if value.architecture_review and (
        value.inspection_mode is not None
        or value.ticket_id is not None
        or value.release_id is not None
        or value.waits
        or value.investigation_json
        or value.verification_json
        or value.human_questions
        or value.execution_enabled
    ):
        raise ValueError("架构评审入口不能混用其他场景或运维执行")
    if value.inspection_mode is not None and (
        value.inspection_mode not in {"inspection", "capacity", "governance"}
        or value.release_id is not None
        or value.ticket_id is not None
        or value.waits
        or value.investigation_json
        or value.verification_json
        or value.human_questions
        or value.execution_enabled
    ):
        raise ValueError("巡检入口必须使用固定类型且不能混用写操作或其他场景")
    if value.release_id is not None and (
        not isinstance(value.release_id, str)
        or not value.release_id.strip()
        or len(value.release_id) > 128
        or value.ticket_id is not None
        or value.waits
        or value.verification_json
        or value.investigation_json
        or value.human_questions
    ):
        raise ValueError("发布入口必须有有效身份且不能混用其他场景")
    if (
        isinstance(value.release_observation_seconds, bool)
        or not math.isfinite(value.release_observation_seconds)
        or not 0 < value.release_observation_seconds <= 3600
    ):
        raise ValueError("发布观测窗口必须为有限正数且最多 3600 秒")
    if value.ticket_id is not None and (
        not isinstance(value.ticket_id, str)
        or not value.ticket_id.strip()
        or len(value.ticket_id) > 256
        or value.waits
        or value.verification_json
        or value.investigation_json
        or value.human_questions
    ):
        raise ValueError("工单入口必须有有效身份且不能混用其他场景输入")
    if type(value.postmortem_enabled) is not bool:
        raise ValueError("postmortem_enabled 必须为宿主布尔配置")
    if type(value.execution_enabled) is not bool:
        raise ValueError("execution_enabled 必须为宿主布尔配置")
    if str(UUID(value.task_id)) != value.task_id:
        raise ValueError("task_id 必须是规范 UUID")
    if len(set(value.waits)) != len(value.waits) or any(
        status not in WAITING_STATUSES for status in value.waits
    ):
        raise ValueError("waits 只能包含三个独立等待状态，且不得重复")
    for seconds in (value.human_timeout_seconds, value.activity_timeout_seconds):
        if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Temporal 超时必须是有限正数")
    if type(value.activity_max_attempts) is not int or not 1 <= value.activity_max_attempts <= 10:
        raise ValueError("Activity 重试次数必须为 1–10")
    if value.investigation_json is not None:
        InvestigationSpec.model_validate_json(value.investigation_json)
        if value.waits:
            raise ValueError("主 Agent 调查不能混用占位等待流程")
    if value.human_questions:
        if value.waits:
            raise ValueError("正式人工问答不能混用占位信号")
        if len(value.human_questions) > 2 or len(
            {q.wait_status for q in value.human_questions}
        ) != len(value.human_questions):
            raise ValueError("同一流程每种人工等待最多配置一次")
        for question in value.human_questions:
            validate_question(question)
    if value.verification_json is not None:
        spec = VerificationSpec.model_validate_json(value.verification_json)
        if str(spec.task_id) != value.task_id:
            raise ValueError("验证规格必须属于当前任务")
        if value.investigation_json is not None or value.waits or value.human_questions:
            raise ValueError("独立验证入口不能混用调查或人工等待输入")


@workflow.defn(name="AITaskWorkflow")
class AITaskWorkflow:
    def __init__(self) -> None:
        self.task: TaskSnapshot | None = None
        self.history: list[TaskSnapshot] = []
        self.response: HumanResponse | None = None
        self.options = WorkflowInput(task_id="")
        self.conclusion_json: str | None = None
        self.conclusion_evidence_id: str | None = None
        self.runbook: RunbookMatchResult | None = None
        self.review_evidence_id: str | None = None
        self.review_json: str | None = None
        self.action_plan_evidence_id: str | None = None
        self.action_plan_json: str | None = None
        self.human_prompt: HumanPrompt | None = None
        self.human_answer: HumanAnswer | None = None
        self.human_answers: list[HumanAnswerResult] = []
        self.human_wait_active = False
        self.approval_prompt: ApprovalPrompt | None = None
        self.approval_response: ApprovalResponse | None = None
        self.approval_result: ApprovalResult | None = None
        self.approval_active = False
        self.verification_evidence_id: str | None = None
        self.verification_json: str | None = None
        self.execution_evidence_ids: list[str] = []
        self.safety_evidence_id: str | None = None
        self.takeover_notification_state: str | None = None
        self.postmortem_evidence_id: str | None = None
        self.postmortem_json: str | None = None
        self.chat_finished = False

    @workflow.update
    async def chat_response(self) -> WorkflowProgress:
        if self.options.chat_mode is None:
            raise ApplicationError("这不是对话任务", non_retryable=True)
        await workflow.wait_condition(
            lambda: (
                self.chat_finished
                or self.approval_prompt is not None
                or (
                    self.action_plan_json is not None
                    and self.task is not None
                    and self.task.status is TaskStatus.WAITING_INFORMATION
                )
            )
        )
        return self.progress()

    @workflow.query
    def progress(self) -> WorkflowProgress:
        return WorkflowProgress(
            self.task,
            list(self.history),
            self.conclusion_json,
            self.conclusion_evidence_id,
            self.review_evidence_id,
            self.review_json,
            self.action_plan_evidence_id,
            self.action_plan_json,
            self.human_prompt,
            list(self.human_answers),
            self.approval_prompt,
            self.approval_result,
            self.verification_evidence_id,
            self.verification_json,
            list(self.execution_evidence_ids),
            self.safety_evidence_id,
            self.takeover_notification_state,
            self.postmortem_evidence_id,
            self.postmortem_json,
            bool(
                self.task
                and self.task.status
                in {TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION}
                and not self.human_wait_active
                and not self.approval_active
            ),
        )

    async def learn(self) -> None:
        assert self.task is not None
        if self.task.status is not TaskStatus.RESOLVED:
            return
        await self.move(TaskStatus.LEARNING, "独立验证通过，自动复盘与沉淀改进")
        result = await workflow.execute_activity(
            "learning.postmortem",
            LearningRequest(self.task),
            result_type=LearningResult,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.postmortem_evidence_id, self.postmortem_json = result.evidence_id, result.report_json
        for receipt in result.improvements:
            await workflow.execute_activity(
                "event.start_task",
                receipt,
                start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
                retry_policy=self.retry_policy(),
            )
        await self.move(TaskStatus.CLOSED, f"复盘与改进任务已留证：{result.evidence_id}")

    async def check_safety(self) -> bool:
        if not workflow.patched("automation-safety-v1"):
            return False
        assert self.task is not None
        result = await workflow.execute_activity(
            "safety.check",
            self.task,
            result_type=SafetyResult,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        if result.evidence_id is None:
            return False
        if self.task != result.task:
            self.task = result.task
            self.history.append(self.task)
        self.safety_evidence_id = result.evidence_id
        self.takeover_notification_state = "pending"
        try:
            await workflow.execute_activity(
                "safety.notify",
                self.task.task_id,
                result_type=str,
                start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
                retry_policy=self.retry_policy(),
            )
            self.takeover_notification_state = "sent"
        except ActivityError:
            # 通知耗尽重试也不能恢复自动化；进度明确报告通知失败。
            self.takeover_notification_state = "failed"
        return True

    @workflow.signal
    def approve_actions(self, response: ApprovalResponse) -> None:
        try:
            validate_response(response)
        except (ValueError, TypeError, AttributeError):
            return
        prompt = self.approval_prompt
        if (
            self.approval_active
            and prompt is not None
            and self.task == prompt.task
            and self.approval_response is None
            and (
                response.task_id,
                response.approval_id,
                response.wait_version,
                response.action_hash,
            )
            == (prompt.task.task_id, prompt.approval_id, prompt.task.version, prompt.action_hash)
        ):
            self.approval_response = response

    @workflow.signal
    def answer_question(self, response: HumanAnswer) -> None:
        try:
            validate_answer(response)
        except (ValueError, TypeError, AttributeError):
            return
        prompt = self.human_prompt
        if (
            self.human_wait_active
            and prompt is not None
            and self.task == prompt.task
            and self.human_answer is None
            and (response.question_id, response.wait_status, response.wait_version)
            == (prompt.question_id, prompt.task.status, prompt.task.version)
        ):
            self.human_answer = response

    @workflow.signal
    def human_response(self, response: HumanResponse) -> None:
        # 只接收当前等待版本的首个回答；旧版本、错误类型、重复信号均不改变结果。
        if (
            self.task is not None
            and not self.human_wait_active
            and not self.approval_active
            and self.task.status in WAITING_STATUSES
            and response.wait_status == self.task.status
            and response.wait_version == self.task.version
            and type(response.accepted) is bool
            and self.response is None
        ):
            self.response = response

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            initial_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=5),
            maximum_attempts=self.options.activity_max_attempts,
        )

    async def move(self, target: TaskStatus, reason: str, *, verifier: bool = False) -> None:
        assert self.task is not None
        self.task = await workflow.execute_activity(
            "verifier.placeholder" if verifier else "task.transition",
            TransitionRequest(self.task, target, reason),
            result_type=TaskSnapshot,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.history.append(self.task)

    async def stage(self) -> None:
        assert self.task is not None
        await workflow.execute_activity(
            "task.placeholder_stage",
            self.task,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )

    async def wait_for_human(
        self,
        status: TaskStatus,
        resume: TaskStatus,
        *,
        reason: str = "等待人工信号（Step 17 占位）",
    ) -> bool:
        self.response = None
        await self.move(status, reason)
        try:
            await workflow.wait_condition(
                lambda: self.response is not None,
                timeout=timedelta(seconds=self.options.human_timeout_seconds),
            )
        except TimeoutError:
            await self.move(TaskStatus.ESCALATED, "人工等待超时，转交人工处理")
            return False
        assert self.response is not None
        if not self.response.accepted:
            await self.move(TaskStatus.ESCALATED, "人工拒绝继续，转交人工处理")
            return False
        await self.move(resume, "收到当前等待版本的人工信号，恢复占位流程")
        self.response = None
        return True

    async def ask_human(self, question: HumanQuestion, resume: TaskStatus) -> bool:
        self.human_wait_active = True
        self.human_answer = None
        self.human_prompt = None
        await self.move(
            question.wait_status,
            "等待业务判断"
            if question.wait_status is TaskStatus.NEED_HUMAN_JUDGMENT
            else "等待补充信息",
        )
        assert self.task is not None
        self.human_prompt = await workflow.execute_activity(
            "human.notify",
            HumanWaitRequest(self.task, question.question, resume),
            result_type=HumanPrompt,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        try:
            await workflow.wait_condition(
                lambda: self.human_answer is not None,
                timeout=timedelta(seconds=self.options.human_timeout_seconds),
            )
        except TimeoutError:
            self.human_wait_active = False
            await self.move(TaskStatus.ESCALATED, "人工问题等待超时，转交人工处理")
            return False
        assert self.human_answer is not None
        result = await workflow.execute_activity(
            "human.record_answer",
            HumanAnswerRequest(self.human_prompt, self.human_answer),
            result_type=HumanAnswerResult,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.human_answers.append(result)
        await self.move(resume, "人工回答已留证并保存 Knowledge 草稿，恢复任务")
        self.human_wait_active = False
        self.human_prompt = None
        self.human_answer = None
        return True

    async def investigate(self, spec_json: str) -> None:
        # 只有 Temporal 决定重新调查及次数；不在 Activity 自造循环状态机。
        for review_round in range(2):
            if not await self.investigation_round(spec_json):
                return
            if not workflow.patched("reviewer-agent-v1"):
                break
            assert self.task is not None and self.conclusion_evidence_id is not None
            review = await workflow.execute_activity(
                "reviewer.review",
                ReviewRequest(self.task, spec_json, self.conclusion_evidence_id),
                result_type=ReviewResult,
                start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
                retry_policy=self.retry_policy(),
            )
            decision = ReviewDecision.model_validate_json(review.decision_json)
            self.review_evidence_id, self.review_json = review.evidence_id, review.decision_json
            if await self.check_safety():
                return
            if decision.report.verdict == "clear":
                break
            if decision.report.verdict == "inconclusive":
                await self.move(TaskStatus.ESCALATED, "Reviewer 替代原因覆盖不足，转交人工处理")
                return
            await self.move(TaskStatus.INVESTIGATING, "Reviewer 找到反证，降低置信度并重新调查")
            self.runbook = None
            if review_round == 1:
                await self.move(TaskStatus.ESCALATED, "两轮复核仍有反证，转交人工处理")
                return
        if self.options.chat_mode == "question" and workflow.patched("chat-question-v1"):
            assert self.task is not None and self.conclusion_evidence_id is not None
            await self.move(TaskStatus.PLANNING, "只读对话经 Reviewer 核对，交付有证据的回答")
            await self.move(TaskStatus.EXECUTING, "仅交付平台回答，不产生运维动作")
            await self.move(TaskStatus.VERIFYING, "由独立 Verifier 核验回答引用及查询审计")
            self.task = await workflow.execute_activity(
                "verifier.chat",
                ChatVerifyRequest(self.task, self.conclusion_evidence_id),
                result_type=TaskSnapshot,
                start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
                retry_policy=self.retry_policy(),
            )
            self.history.append(self.task)
            await self.move(TaskStatus.LEARNING, "保留对话事实与结论供追问和审计")
            await self.move(TaskStatus.CLOSED, "只读对话完成，未执行运维动作")
            return
        if self.review_evidence_id and workflow.patched("action-plan-v1"):
            await self.plan_actions(spec_json)
            return
        self.response = None
        await self.move(
            TaskStatus.WAITING_INFORMATION,
            "RCA 与 Reviewer 已留证；等待 Step 28 Action Plan 阶段接入"
            if self.review_evidence_id
            else "RCA 结论已留证；等待后续 Reviewer 与 Action Plan 阶段接入",
        )
        try:
            await workflow.wait_condition(
                lambda: self.response is not None,
                timeout=timedelta(seconds=self.options.human_timeout_seconds),
            )
        except TimeoutError:
            await self.move(TaskStatus.ESCALATED, "RCA 后等待超时，转交人工处理")
            return
        await self.move(TaskStatus.ESCALATED, "后续处置阶段尚未接入，转交人工处理")

    async def plan_actions(self, spec_json: str) -> None:
        assert self.task and self.conclusion_evidence_id and self.review_evidence_id
        await self.move(TaskStatus.PLANNING, "当前 RCA 通过 Reviewer，生成结构化处置计划")
        result = await workflow.execute_activity(
            "task.plan_actions",
            PlanningRequest(
                self.task, spec_json, self.conclusion_evidence_id, self.review_evidence_id
            ),
            result_type=PlanningResult,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        plan = ActionPlan.model_validate_json(result.plan_json)
        self.action_plan_evidence_id, self.action_plan_json = result.evidence_id, result.plan_json
        if plan.decision is PolicyDecision.DENY:
            await self.move(TaskStatus.ESCALATED, "处置计划含 Policy 禁止动作，转交人工处理")
            return
        if (
            plan.decision is PolicyDecision.ALLOW
            and self.options.execution_enabled
            and workflow.patched("executor-v1")
        ):
            await self.move(TaskStatus.EXECUTING, "Policy 已放行，进入 Executor")
            await self.execute_actions()
            return
        self.response = None
        approval_enabled = plan.decision is PolicyDecision.NEED_APPROVAL and workflow.patched(
            "action-approval-v1"
        )
        self.approval_active = approval_enabled
        await self.move(
            TaskStatus.WAITING_APPROVAL
            if plan.decision is PolicyDecision.NEED_APPROVAL
            else TaskStatus.WAITING_INFORMATION,
            "处置计划需要与动作哈希绑定的独立审批"
            if approval_enabled
            else "处置计划需要审批；审批流与执行留待后续步骤"
            if plan.decision is PolicyDecision.NEED_APPROVAL
            else "Policy 已放行候选计划；等待后续 Executor 接入",
        )
        if approval_enabled:
            await self.wait_for_approval()
            return
        try:
            await workflow.wait_condition(
                lambda: self.response is not None,
                timeout=timedelta(seconds=self.options.human_timeout_seconds),
            )
        except TimeoutError:
            await self.move(TaskStatus.ESCALATED, "处置计划等待超时，转交人工处理")
            return
        # Step 30/32 尚未接入，Step 17 的布尔信号不是有效动作审批。
        await self.move(TaskStatus.ESCALATED, "审批与执行尚未接入，人工信号转交人工处理")

    async def wait_for_approval(self) -> None:
        assert self.task is not None and self.action_plan_evidence_id is not None
        self.approval_prompt = await workflow.execute_activity(
            "approval.notify",
            ApprovalRequest(self.task, self.action_plan_evidence_id),
            result_type=ApprovalPrompt,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        try:
            await workflow.wait_condition(
                lambda: self.approval_response is not None,
                timeout=timedelta(seconds=self.options.human_timeout_seconds),
            )
        except TimeoutError:
            pass
        self.approval_result = await workflow.execute_activity(
            "approval.decide",
            ApprovalDecisionRequest(self.approval_prompt, self.approval_response),
            result_type=ApprovalResult,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.approval_active = False
        if self.approval_result.decision == "approved":
            await self.move(TaskStatus.EXECUTING, "动作审批已留证；等待 Step 32 Executor 接入")
            if self.options.release_id is not None or self.options.war_room:
                return
            elif self.options.ticket_id is not None:
                await self.execute_ticket_actions()
            elif self.options.execution_enabled and workflow.patched("executor-v1"):
                await self.execute_actions()
        else:
            await self.move(
                TaskStatus.ESCALATED,
                "审批被拒绝，转交人工处理"
                if self.approval_result.decision == "rejected"
                else "审批等待超时，转交人工处理",
            )

    async def execute_actions(self) -> None:
        assert self.task is not None and self.action_plan_evidence_id is not None
        result = await workflow.execute_activity(
            "executor.execute_action",
            ExecutionRequest(self.task, self.action_plan_evidence_id, self.approval_prompt),
            result_type=ExecutionResult,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.task = result.task
        self.execution_evidence_ids = result.evidence_ids
        self.history.append(self.task)
        if await self.check_safety():
            return
        if self.options.postmortem_enabled and workflow.patched("postmortem-v1"):
            spec_json = await workflow.execute_activity(
                "verifier.prepare_after_execution",
                self.task,
                result_type=str,
                start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
                retry_policy=self.retry_policy(),
            )
            spec = VerificationSpec.model_validate_json(spec_json)
            if spec.end > workflow.now():
                await workflow.sleep(spec.end - workflow.now())
            verification = await workflow.execute_activity(
                "verifier.verify_action",
                VerificationRequest(self.task, spec_json),
                result_type=VerificationResult,
                start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
                retry_policy=self.retry_policy(),
            )
            self.task = verification.task
            self.verification_evidence_id = verification.evidence_id
            self.verification_json = verification.report_json
            self.history.append(self.task)
            if not await self.check_safety():
                await self.learn()

    async def investigation_round(self, spec_json: str) -> bool:
        assert self.task is not None
        result = await workflow.execute_activity(
            "agent.investigate",
            InvestigationRequest(
                self.task,
                spec_json,
                self.runbook.runbook_json if self.runbook else None,
                self.review_evidence_id,
            ),
            result_type=InvestigationResult,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        if await self.check_safety():
            return False
        await self.move(TaskStatus.RCA, "主 Agent 调查完成，进入证据校验")
        self.conclusion_evidence_id = await workflow.execute_activity(
            "agent.validate_conclusion",
            ConclusionRequest(self.task, result),
            result_type=str,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.conclusion_json = result.conclusion_json
        return True

    async def ticket_activity[Result](
        self, name: str, request: object, result_type: type[Result]
    ) -> Result:
        result = await workflow.execute_activity(
            name,
            request,
            result_type=result_type,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        return cast(Result, result)

    async def run_release(self) -> None:
        assert self.task is not None and self.options.release_id is not None
        await self.move(TaskStatus.CONTEXT_BUILDING, "读取发布申请并构建服务影响面")
        await self.move(TaskStatus.RUNBOOK_MATCHING, "发布优先检索并验证 Runbook 适用性")
        # 服务名由源申请绑定，Activity 内检查，不使用模型提供的执行目标。
        self.runbook = await self.ticket_activity(
            "release.match_runbook",
            ReleaseStageRequest(self.task, self.options.release_id),
            RunbookMatchResult,
        )
        if self.runbook.blocked or await self.check_safety():
            if not self.safety_evidence_id:
                await self.move(TaskStatus.ESCALATED, self.runbook.reason)
            return
        await self.move(TaskStatus.INVESTIGATING, self.runbook.reason)
        purpose: Purpose = "canary"
        for _ in range(4):
            await self.move(TaskStatus.RCA, "发布预检查或异常调查形成证据结论")
            assert self.task is not None
            assessment_id = await self.ticket_activity(
                "release.assess",
                ReleaseStageRequest(self.task, self.options.release_id, purpose),
                str,
            )
            assessment_json = await self.ticket_activity(
                "release.read_assessment", assessment_id, str
            )
            assessment = ReleaseAssessment.model_validate_json(assessment_json)
            self.conclusion_evidence_id, self.conclusion_json = assessment_id, assessment_json
            if not assessment.passed:
                failed = "、".join(c.name for c in assessment.checks if not c.passed)
                if await self.ask_human(
                    HumanQuestion(
                        TaskStatus.NEED_HUMAN_JUDGMENT,
                        "发布检查未通过："
                        + failed
                        + "。SQL 变更至少按 L4 标记，需独立评审；回答不构成发布或 SQL 授权。",
                    ),
                    TaskStatus.RCA,
                ):
                    await self.move(
                        TaskStatus.ESCALATED, "检查未通过，人工判断已保存；须修订源申请后重新发起"
                    )
                return
            self.review_evidence_id = await self.ticket_activity(
                "release.review", ReleasePlanRequest(self.task, assessment_id), str
            )
            if await self.check_safety():
                return
            await self.move(TaskStatus.PLANNING, "发布独立 Reviewer 通过，形成精确阶段动作")
            result = await self.ticket_activity(
                "release.plan",
                ReleasePlanRequest(self.task, assessment_id, self.review_evidence_id),
                PlanningResult,
            )
            plan = ActionPlan.model_validate_json(result.plan_json)
            self.action_plan_evidence_id, self.action_plan_json = (
                result.evidence_id,
                result.plan_json,
            )
            self.approval_prompt = None
            self.approval_response = None
            self.approval_result = None
            if plan.decision is PolicyDecision.DENY:
                await self.move(TaskStatus.ESCALATED, "Policy 禁止发布阶段动作")
                return
            if plan.decision is PolicyDecision.NEED_APPROVAL:
                self.approval_active = True
                await self.move(
                    TaskStatus.WAITING_APPROVAL, "发布阶段 " + purpose + " 需要动作哈希绑定审批"
                )
                await self.wait_for_approval()
                if self.task.status is not TaskStatus.EXECUTING:
                    return
            else:
                await self.move(TaskStatus.EXECUTING, "Policy 显式放行发布阶段 " + purpose)
            if not self.options.execution_enabled:
                await self.move(TaskStatus.ESCALATED, "Executor 未启用，发布动作零执行")
                return
            executed = await self.ticket_activity(
                "release.execute",
                ExecutionRequest(self.task, result.evidence_id, self.approval_prompt),
                ExecutionResult,
            )
            self.task = executed.task
            self.history.append(self.task)
            self.execution_evidence_ids.extend(executed.evidence_ids)
            if await self.check_safety():
                return
            start = workflow.now()
            end = start + timedelta(seconds=self.options.release_observation_seconds)
            await workflow.sleep(end - start)
            observed = await self.ticket_activity(
                "release.observe",
                ReleaseObserveRequest(
                    self.task,
                    self.options.release_id,
                    start.isoformat(),
                    end.isoformat(),
                    result.evidence_id,
                    final=purpose == "rollback",
                ),
                ReleaseObserveResult,
            )
            self.task = observed.task
            self.verification_evidence_id, self.verification_json = (
                observed.evidence_id,
                observed.report_json,
            )
            if self.history[-1] != self.task:
                self.history.append(self.task)
            if await self.check_safety():
                return
            if purpose == "pause":
                if not json.loads(observed.report_json)["target_matches"]:
                    await self.move(TaskStatus.ESCALATED, "暂停未独立读回，停止回滚并转人工")
                    return
                purpose = "rollback"
            elif purpose == "rollback":
                if not observed.passed:
                    await self.move(TaskStatus.ESCALATED, "回滚后仍未恢复，停止自动尝试并转人工")
                    return
                break
            elif observed.passed and purpose == "promote":
                observed = await self.ticket_activity(
                    "release.observe",
                    ReleaseObserveRequest(
                        self.task,
                        self.options.release_id,
                        start.isoformat(),
                        end.isoformat(),
                        result.evidence_id,
                        final=True,
                    ),
                    ReleaseObserveResult,
                )
                self.task = observed.task
                self.history.append(self.task)
                self.verification_evidence_id, self.verification_json = (
                    observed.evidence_id,
                    observed.report_json,
                )
                if not observed.passed:
                    await self.move(TaskStatus.ESCALATED, "最终验证发现变化，停止发布并转人工")
                    return
                break
            elif observed.passed:
                purpose = "promote"
            else:
                purpose = "pause"
            await self.move(
                TaskStatus.INVESTIGATING,
                "发布窗口异常，停止推广并调查暂停/回滚"
                if purpose == "pause"
                else "当前阶段已读回，调查下一发布阶段",
            )
        else:
            await self.move(TaskStatus.ESCALATED, "发布阶段次数耗尽，转人工")
            return
        await self.move(TaskStatus.LEARNING, "发布或回滚经独立验证，保存发布报告")
        self.postmortem_evidence_id = await self.ticket_activity("release.report", self.task, str)
        await self.move(TaskStatus.CLOSED, "发布报告与完整证据链已保存")

    async def run_ticket(self) -> None:
        assert self.task is not None and self.options.ticket_id is not None
        await self.move(TaskStatus.CONTEXT_BUILDING, "工单分类、读取服务 Context 并检查信息完整性")
        context_json = ""
        for _ in range(3):
            context_json = await self.ticket_activity(
                "ticket.prepare", TicketStageRequest(self.task, self.options.ticket_id), str
            )
            context = TicketContext.model_validate_json(context_json)
            if context.category is not TicketCategory.PERMISSION or not context.missing:
                break
            if not await self.ask_human(
                HumanQuestion(
                    TaskStatus.WAITING_INFORMATION,
                    "权限工单缺少 "
                    + "、".join(context.missing)
                    + "。请以 JSON 补充缺失字段：subject_id/resource/permission/"
                    "expires_at（UTC）/reason；已有字段不能改写。",
                ),
                TaskStatus.CONTEXT_BUILDING,
            ):
                return
        else:
            await self.move(TaskStatus.ESCALATED, "三轮补充后权限工单信息仍不完整")
            return
        await self.move(TaskStatus.RUNBOOK_MATCHING, "工单优先检索并校验适用 Runbook")
        spec = InvestigationSpec(
            service_name=context.service_name,
            title="权限工单 " + context.ticket_id,
            start=workflow.now() - timedelta(seconds=1),
            end=workflow.now(),
            max_steps=20,
        )
        self.runbook = await self.ticket_activity(
            "runbook.match",
            RunbookMatchRequest(self.task, spec.model_dump_json()),
            RunbookMatchResult,
        )
        if self.runbook.blocked or await self.check_safety():
            if not self.safety_evidence_id:
                await self.move(TaskStatus.ESCALATED, self.runbook.reason)
            return
        await self.move(TaskStatus.INVESTIGATING, self.runbook.reason)
        if context.category is not TicketCategory.PERMISSION or context.judgment:
            question = (
                context.judgment
                or "该类工单的写动作尚不在当前执行白名单，请判断处理方案或转交人工；"
                "不能用回答代替动作审批。"
            )
            if await self.ask_human(
                HumanQuestion(TaskStatus.NEED_HUMAN_JUDGMENT, question), TaskStatus.INVESTIGATING
            ):
                await self.move(
                    TaskStatus.ESCALATED, "人工判断已留证，待安全动作适配或宿主权限规则明确后处理"
                )
            return
        result = await self.ticket_activity(
            "ticket.investigate",
            TicketAnalysisRequest(self.task, context_json, self.runbook.runbook_json),
            InvestigationResult,
        )
        await self.move(TaskStatus.RCA, "工单主 Agent 分析完成，校验引用证据")
        self.conclusion_evidence_id = await self.ticket_activity(
            "ticket.conclude", ConclusionRequest(self.task, result), str
        )
        self.conclusion_json = result.conclusion_json
        self.review_evidence_id = await self.ticket_activity(
            "ticket.review",
            TicketPlanRequest(self.task, context_json, self.conclusion_evidence_id, ""),
            str,
        )
        await self.move(TaskStatus.PLANNING, "独立工单 Reviewer 通过，形成权限与回填关闭动作计划")
        plan = await self.ticket_activity(
            "ticket.plan",
            TicketPlanRequest(
                self.task, context_json, self.conclusion_evidence_id, self.review_evidence_id
            ),
            PlanningResult,
        )
        self.action_plan_evidence_id, self.action_plan_json = plan.evidence_id, plan.plan_json
        decision = ActionPlan.model_validate_json(plan.plan_json).decision
        if decision is PolicyDecision.DENY:
            await self.move(TaskStatus.ESCALATED, "Policy 禁止工单动作")
        elif decision is PolicyDecision.ALLOW:
            await self.move(TaskStatus.EXECUTING, "Policy 放行完整工单计划")
            await self.execute_ticket_actions()
        else:
            self.approval_active = True
            await self.move(TaskStatus.WAITING_APPROVAL, "权限与工单回填关闭均需要精确动作审批")
            await self.wait_for_approval()

    async def execute_ticket_actions(self) -> None:
        assert (
            self.task is not None
            and self.action_plan_evidence_id is not None
            and self.options.ticket_id is not None
        )
        if not self.options.execution_enabled:
            await self.move(TaskStatus.ESCALATED, "Executor 未启用，工单动作零执行")
            return
        execution_id = await self.ticket_activity(
            "ticket.execute",
            TicketExecutionRequest(
                self.task, self.action_plan_evidence_id, self.approval_prompt, 0
            ),
            str,
        )
        self.execution_evidence_ids.append(execution_id)
        await self.move(TaskStatus.VERIFYING, "权限动作已提交，独立验证后才允许关闭工单")
        verified = await self.ticket_activity(
            "ticket.verify",
            TicketVerifyRequest(self.task, self.action_plan_evidence_id),
            TicketVerifyResult,
        )
        if not verified.passed:
            self.task = verified.task
            self.history.append(self.task)
            return
        if await self.check_safety():
            return
        closed_id = await self.ticket_activity(
            "ticket.execute",
            TicketExecutionRequest(
                self.task, self.action_plan_evidence_id, self.approval_prompt, 1
            ),
            str,
        )
        self.execution_evidence_ids.append(closed_id)
        verified = await self.ticket_activity(
            "ticket.verify",
            TicketVerifyRequest(self.task, self.action_plan_evidence_id, True),
            TicketVerifyResult,
        )
        self.task = verified.task
        self.history.append(self.task)
        self.verification_evidence_id = verified.evidence_id
        if not verified.passed or await self.check_safety():
            return
        await self.move(TaskStatus.LEARNING, "工单权限与带证据回填关闭已独立验证，沉淀学习")
        self.postmortem_evidence_id = await self.ticket_activity(
            "ticket.learn", TicketStageRequest(self.task, self.options.ticket_id), str
        )
        await self.move(TaskStatus.CLOSED, "工单闭环完成，经验已保存为待审核 Runbook 草稿")

    @workflow.run
    async def run(self, value: WorkflowInput) -> WorkflowProgress:
        try:
            validate_workflow_input(value)
        except (ValueError, TypeError) as error:
            raise ApplicationError(str(error), non_retryable=True) from None
        self.options = value
        if value.verification_json is not None:
            spec = VerificationSpec.model_validate_json(value.verification_json)
            self.task = TaskSnapshot(value.task_id, TaskStatus.VERIFYING, spec.verifying_version)
            self.history.append(self.task)
            try:
                result = await workflow.execute_activity(
                    "verifier.verify_action",
                    VerificationRequest(self.task, value.verification_json),
                    result_type=VerificationResult,
                    start_to_close_timeout=timedelta(seconds=value.activity_timeout_seconds),
                    retry_policy=self.retry_policy(),
                )
                self.task = result.task
                self.verification_evidence_id, self.verification_json = (
                    result.evidence_id,
                    result.report_json,
                )
                self.history.append(self.task)
                await self.check_safety()
                if value.postmortem_enabled and workflow.patched("postmortem-v1"):
                    await self.learn()
            except ActivityError:
                if await self.check_safety():
                    return self.progress()
                await self.move(TaskStatus.ESCALATED, "独立验证无法完成，转交人工处理")
            return self.progress()
        self.task = await workflow.execute_activity(
            "task.load",
            value.task_id,
            result_type=TaskSnapshot,
            start_to_close_timeout=timedelta(seconds=value.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.history.append(self.task)
        try:
            if value.war_room and workflow.patched("war-room-v1"):
                await self.run_war_room()
                return self.progress()
            if value.architecture_review and workflow.patched("architecture-review-v1"):
                await self.run_architecture_review()
                return self.progress()
            if value.inspection_mode is not None and workflow.patched("inspection-scenario-v1"):
                await self.run_inspection()
                return self.progress()
            if value.release_id is not None and workflow.patched("release-scenario-v1"):
                await self.run_release()
                return self.progress()
            if value.ticket_id is not None and workflow.patched("ticket-scenario-v1"):
                await self.run_ticket()
                return self.progress()
            for status in (
                TaskStatus.CONTEXT_BUILDING,
                TaskStatus.RUNBOOK_MATCHING,
                TaskStatus.INVESTIGATING,
                TaskStatus.RCA,
                TaskStatus.PLANNING,
            ):
                await self.move(
                    status,
                    self.runbook.reason
                    if self.runbook is not None and status is TaskStatus.INVESTIGATING
                    else "进入占位阶段",
                )
                human_wait = {
                    TaskStatus.CONTEXT_BUILDING: TaskStatus.WAITING_INFORMATION,
                    TaskStatus.INVESTIGATING: TaskStatus.NEED_HUMAN_JUDGMENT,
                }.get(status)
                question = next(
                    (q for q in value.human_questions if q.wait_status is human_wait), None
                )
                if question is not None and workflow.patched("human-interaction-v1"):
                    if not await self.ask_human(question, status):
                        return self.progress()
                if (
                    status is TaskStatus.RUNBOOK_MATCHING
                    and value.investigation_json is not None
                    and workflow.patched("runbook-engine-v1")
                ):
                    self.runbook = await workflow.execute_activity(
                        "runbook.match",
                        RunbookMatchRequest(self.task, value.investigation_json),
                        result_type=RunbookMatchResult,
                        start_to_close_timeout=timedelta(seconds=value.activity_timeout_seconds),
                        retry_policy=self.retry_policy(),
                    )
                    if await self.check_safety():
                        return self.progress()
                    if self.runbook.blocked:
                        await self.move(TaskStatus.ESCALATED, self.runbook.reason)
                        return self.progress()
                    continue
                if status is TaskStatus.INVESTIGATING and value.investigation_json is not None:
                    await self.investigate(value.investigation_json)
                    if value.chat_mode is not None:
                        self.chat_finished = True
                        await workflow.wait_condition(workflow.all_handlers_finished)
                    return self.progress()
                await self.stage()
                waiting = {
                    TaskStatus.CONTEXT_BUILDING: TaskStatus.WAITING_INFORMATION,
                    TaskStatus.INVESTIGATING: TaskStatus.NEED_HUMAN_JUDGMENT,
                    TaskStatus.PLANNING: TaskStatus.WAITING_APPROVAL,
                }.get(status)
                if waiting in value.waits:
                    assert waiting is not None
                    resume = (
                        TaskStatus.EXECUTING if waiting is TaskStatus.WAITING_APPROVAL else status
                    )
                    if not await self.wait_for_human(waiting, resume):
                        return self.progress()
            if self.task.status is not TaskStatus.EXECUTING:
                await self.move(TaskStatus.EXECUTING, "进入无运维副作用的占位执行阶段")
            await self.stage()
            await self.move(TaskStatus.VERIFYING, "进入独立占位验证阶段")
            await self.move(TaskStatus.RESOLVED, "本地占位验证通过", verifier=True)
            if self.task.status is not TaskStatus.RESOLVED:
                # 兼容入口现在也执行独立验证；未恢复必须停在 INVESTIGATING。
                return self.progress()
            await self.move(TaskStatus.LEARNING, "进入占位学习阶段")
            await self.stage()
            await self.move(TaskStatus.CLOSED, "Step 17 占位流程完成")
        except (ActivityError, ChildWorkflowError) as error:
            if await self.check_safety():
                if value.chat_mode is not None:
                    self.chat_finished = True
                    await workflow.wait_condition(workflow.all_handlers_finished)
                return self.progress()
            # Activity 重试已由 Temporal 耗尽；不在进程中自造重试或调度器。
            reason = (
                "主 Agent 超过最大调查步数，转交人工处理"
                if isinstance(error.cause, ApplicationError)
                and error.cause.type == "AgentStepLimit"
                else "Reviewer 超过最大复核步数，转交人工处理"
                if isinstance(error.cause, ApplicationError)
                and error.cause.type == "ReviewerStepLimit"
                else "Activity 失败或结论被拒绝，转交人工处理"
                if value.investigation_json is not None
                else "Activity 失败，转交人工处理"
            )
            await self.move(TaskStatus.ESCALATED, reason)
        if value.chat_mode is not None:
            self.chat_finished = True
            await workflow.wait_condition(workflow.all_handlers_finished)
        return self.progress()

    async def war_room_action(self, assessment: WarRoomResult) -> bool:
        assert self.task is not None
        review_id = await self.ticket_activity(
            "war_room.review", WarRoomVerifyRequest(self.task, assessment.evidence_id), str
        )
        if not review_id:
            await self.move(TaskStatus.ESCALATED, "保障 Reviewer 查询失败或被拒绝")
            return False
        self.review_evidence_id = review_id
        await self.move(TaskStatus.PLANNING, "重大保障独立 Reviewer 反证通过，生成容量动作")
        plan = await self.ticket_activity(
            "war_room.plan", WarRoomVerifyRequest(self.task, assessment.evidence_id), PlanningResult
        )
        self.action_plan_evidence_id, self.action_plan_json = plan.evidence_id, plan.plan_json
        decision = ActionPlan.model_validate_json(plan.plan_json).decision
        if decision is PolicyDecision.DENY:
            await self.move(TaskStatus.ESCALATED, "Policy 禁止重大保障容量动作")
            return False
        self.approval_prompt = None
        self.approval_response = None
        self.approval_result = None
        if decision is PolicyDecision.NEED_APPROVAL:
            self.approval_active = True
            await self.move(TaskStatus.WAITING_APPROVAL, "重大保障容量动作需精确参数哈希审批")
            await self.wait_for_approval()
            if self.task.status is not TaskStatus.EXECUTING:
                return False
        else:
            await self.move(TaskStatus.EXECUTING, "Policy 放行重大保障容量动作")
        if not self.options.execution_enabled:
            await self.move(TaskStatus.ESCALATED, "Executor 未启用，保障容量动作零执行")
            return False
        executed = await self.ticket_activity(
            "executor.execute_action",
            ExecutionRequest(self.task, plan.evidence_id, self.approval_prompt),
            ExecutionResult,
        )
        self.task = executed.task
        self.history.append(self.task)
        self.execution_evidence_ids.extend(executed.evidence_ids)
        return not await self.check_safety()

    async def run_war_room(self) -> None:
        assert self.task is not None
        await self.move(TaskStatus.CONTEXT_BUILDING, "读取重大保障活动、服务与 UTC 时间范围")
        await self.move(TaskStatus.RUNBOOK_MATCHING, "重大保障优先 Runbook 检索与适用/排除检查")
        await self.move(TaskStatus.INVESTIGATING, "容量评估、监控/告警/回滚检查与风险扫描")
        await self.move(TaskStatus.RCA, "保存重大保障准备清单与容量结论")
        for _ in range(3):
            initial = await self.ticket_activity(
                "war_room.assess", WarRoomRequest(self.task), WarRoomResult
            )
            if initial.blocked:
                await self.move(TaskStatus.ESCALATED, "保障查询失败或被拒绝，审计已保留")
                return
            check = WarRoomAssessment.model_validate_json(initial.report_json)
            self.conclusion_evidence_id, self.conclusion_json = (
                initial.evidence_id,
                initial.report_json,
            )
            if check.complete:
                break
            if not await self.ask_human(
                HumanQuestion(
                    TaskStatus.WAITING_INFORMATION,
                    "保障容量、已审核适用 Runbook 或监控事实缺失；请补齐来源数据，回答后重新采集。",
                ),
                TaskStatus.RCA,
            ):
                return
        else:
            await self.move(TaskStatus.ESCALATED, "三轮补充后保障检查仍不完整")
            return
        if not check.safe:
            if await self.ask_human(
                HumanQuestion(
                    TaskStatus.NEED_HUMAN_JUDGMENT,
                    "重大保障风险检查未通过或超过容量上限，请判断延期/调整活动；回答不授权资源变更。",
                ),
                TaskStatus.RCA,
            ):
                await self.move(TaskStatus.ESCALATED, "保障风险仍需修订，人工判断已保存")
            return
        if check.required_replicas > check.target.replicas:
            if not await self.war_room_action(initial):
                return
        else:
            self.review_evidence_id = await self.ticket_activity(
                "war_room.review", WarRoomVerifyRequest(self.task, initial.evidence_id), str
            )
            await self.move(TaskStatus.PLANNING, "现有容量满足保障，Reviewer 核对后无需资源写入")
            await self.move(TaskStatus.EXECUTING, "仅完成保障准备记录，无需扩容")
            await self.move(TaskStatus.VERIFYING, "独立核验保障准备状态")
        ready = await self.ticket_activity(
            "verifier.war_room",
            WarRoomVerifyRequest(self.task, initial.evidence_id),
            WarRoomVerified,
        )
        self.verification_evidence_id = ready.evidence_id
        if not ready.passed or await self.check_safety():
            if not self.safety_evidence_id:
                await self.move(TaskStatus.ESCALATED, "资源准备未独立核验通过，保留资源并转人工")
            return
        value_json = await self.ticket_activity("war_room.input", self.task.task_id, str)
        value = WarRoomSubmission.model_validate_json(value_json)
        if workflow.now() < value.start:
            await workflow.sleep(value.start - workflow.now())
        cursor = value.start
        interval = await self.ticket_activity("war_room.interval", self.task.task_id, float)
        window = 0
        while cursor < value.end:
            end = min(cursor + timedelta(seconds=interval), value.end)
            if end > workflow.now():
                await workflow.sleep(end - workflow.now())
            observed = await self.ticket_activity(
                "war_room.assess",
                WarRoomRequest(self.task, "watch", window, cursor.isoformat(), end.isoformat()),
                WarRoomResult,
            )
            if observed.blocked:
                await self.move(TaskStatus.ESCALATED, "实时盯盘查询失败，保留资源并转人工")
                return
            self.conclusion_evidence_id, self.conclusion_json = (
                observed.evidence_id,
                observed.report_json,
            )
            for receipt in observed.receipts:
                await self.ticket_activity("event.start_task", receipt, type(None))
            if await self.check_safety():
                return
            cursor, window = end, window + 1
        await self.move(TaskStatus.INVESTIGATING, "保障期结束，检查本次临时资源归属与回收后容量")
        await self.move(TaskStatus.RCA, "资源回收结论与结束时间重新留证")
        cleanup = await self.ticket_activity(
            "war_room.assess", WarRoomRequest(self.task, "cleanup"), WarRoomResult
        )
        if cleanup.blocked:
            await self.move(TaskStatus.ESCALATED, "回收检查被拒，保留临时资源")
            return
        check = WarRoomAssessment.model_validate_json(cleanup.report_json)
        self.conclusion_evidence_id, self.conclusion_json = cleanup.evidence_id, cleanup.report_json
        if not check.safe:
            await self.move(
                TaskStatus.ESCALATED, "回收容量不足、风险未恢复或资源被其他变更修改，保留资源转人工"
            )
            return
        if check.target.replicas != check.baseline.replicas:
            if not await self.war_room_action(cleanup):
                return
        else:
            self.review_evidence_id = await self.ticket_activity(
                "war_room.review", WarRoomVerifyRequest(self.task, cleanup.evidence_id), str
            )
            await self.move(TaskStatus.PLANNING, "无本次临时副本需要回收，独立 Reviewer 核对")
            await self.move(TaskStatus.EXECUTING, "交付保障记录，无需回收写操作")
            await self.move(TaskStatus.VERIFYING, "独立核验保障结束与业务健康")
        verified = await self.ticket_activity(
            "verifier.war_room",
            WarRoomVerifyRequest(self.task, cleanup.evidence_id, True),
            WarRoomVerified,
        )
        self.task = verified.task
        if self.history[-1] != self.task:
            self.history.append(self.task)
        self.verification_evidence_id = verified.evidence_id
        if not verified.passed or await self.check_safety():
            if not self.safety_evidence_id:
                await self.move(TaskStatus.ESCALATED, "保障结束后业务或回收结果未恢复，转人工")
            return
        await self.move(TaskStatus.LEARNING, "保障及资源回收独立验证成功，保存完整保障报告")
        self.postmortem_evidence_id = await self.ticket_activity("war_room.report", self.task, str)
        await self.move(TaskStatus.CLOSED, "重大保障闭环完成，十一项报告与异常处置任务可追踪")

    async def run_architecture_review(self) -> None:
        assert self.task is not None
        await self.move(TaskStatus.CONTEXT_BUILDING, "读取已归一化的技术方案输入快照")
        await self.move(TaskStatus.RUNBOOK_MATCHING, "架构评审优先检索并核对 Runbook")
        await self.move(
            TaskStatus.INVESTIGATING, "查询 Context Graph、公司规范与历史故障并评审十二个维度"
        )
        result = await workflow.execute_activity(
            "architecture.review",
            ArchitectureRequest(self.task),
            result_type=ArchitectureResult,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.conclusion_evidence_id, self.conclusion_json = result.evidence_id, result.report_json
        if result.blocked:
            await self.move(TaskStatus.ESCALATED, "架构评审所需查询被拒或失败，保留审计并转人工")
            return
        await self.move(TaskStatus.RCA, f"十二维架构评审报告已留证：{result.evidence_id}")
        await self.move(TaskStatus.PLANNING, "评审意见与补充材料建议已保存，不生成运维动作")
        await self.move(TaskStatus.EXECUTING, "完成平台内部报告交付，无运维副作用")
        await self.move(TaskStatus.VERIFYING, "独立核对输入、十二维覆盖与真实 Evidence 引用")
        verified = await workflow.execute_activity(
            "verifier.architecture",
            ArchitectureVerifyRequest(self.task, result.evidence_id),
            result_type=TaskSnapshot,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.task = verified
        self.history.append(verified)
        await self.move(TaskStatus.LEARNING, "保留架构评审及核验记录供后续查询")
        await self.move(TaskStatus.CLOSED, "架构评审任务完成，风险和待补充项均在报告中保留")

    async def run_inspection(self) -> None:
        assert self.task is not None and self.options.inspection_mode is not None
        mode = self.options.inspection_mode
        if mode not in {"inspection", "capacity", "governance"}:
            raise ValueError("巡检类型无效")
        await self.move(TaskStatus.CONTEXT_BUILDING, "加载环境配置中的巡检范围")
        await self.move(TaskStatus.RUNBOOK_MATCHING, "优先检索并核对巡检 Runbook")
        await self.move(TaskStatus.INVESTIGATING, "经 L0 Tool 查询巡检与治理事实")
        while True:
            result = await workflow.execute_child_workflow(
                InspectionWorkflow.run,
                InspectionRequest(self.task, cast(InspectionMode, mode)),
                id=f"{workflow.info().workflow_id}/inspection/{self.task.version}",
            )
            self.conclusion_evidence_id, self.conclusion_json = (
                result.evidence_id,
                result.report_json,
            )
            report = InspectionReport.model_validate_json(result.report_json)
            await self.move(TaskStatus.RCA, f"巡检结论引用 Evidence: {result.evidence_id}")
            if report.complete:
                break
            if not await self.wait_for_human(
                TaskStatus.WAITING_INFORMATION,
                TaskStatus.INVESTIGATING,
                reason="巡检事实缺失、过期或查询失败，等待数据恢复信号后重新采集",
            ):
                return
        await self.move(TaskStatus.PLANNING, "只读扫描完成，异常保存为风险，处理另走授权链路")
        await self.move(TaskStatus.EXECUTING, "仅完成平台内部风险与报告记录，无运维写操作")
        await self.move(TaskStatus.VERIFYING, "独立核验巡检证据与风险落库")
        verified = await workflow.execute_activity(
            "verifier.inspection",
            InspectionVerifyRequest(self.task, result.evidence_id),
            result_type=TaskSnapshot,
            start_to_close_timeout=timedelta(seconds=self.options.activity_timeout_seconds),
            retry_policy=self.retry_policy(),
        )
        self.task = verified
        self.history.append(verified)
        await self.move(TaskStatus.LEARNING, "保留本次巡检报告与风险变化证据")
        await self.move(TaskStatus.CLOSED, "巡检任务完成；未修复风险仍保持打开")
