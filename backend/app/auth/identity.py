"""从已验证会话派生人工操作身份，HTTP 请求体不能提供操作人。"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.tasks.workflow_models import ApprovalPrompt, ApprovalResponse, HumanAnswer, HumanPrompt


@dataclass(frozen=True)
class Principal:
    actor: str
    session_id: UUID
    expires_at: datetime
    csrf_token: str = field(repr=False)

    def approval_response(self, prompt: ApprovalPrompt, decision: str) -> ApprovalResponse:
        return ApprovalResponse(
            prompt.task.task_id,
            prompt.approval_id,
            prompt.task.version,
            prompt.action_hash,
            decision,
            self.actor,
        )

    def human_answer(self, prompt: HumanPrompt, answer: str) -> HumanAnswer:
        return HumanAnswer(
            prompt.question_id, prompt.task.status, prompt.task.version, answer, self.actor
        )
