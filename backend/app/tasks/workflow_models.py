"""Temporal 的纯数据契约；不导入数据库、配置或外部系统。"""

from dataclasses import dataclass, field

from app.tasks.states import TaskStatus


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    status: TaskStatus
    version: int


@dataclass(frozen=True)
class HumanQuestion:
    wait_status: TaskStatus
    question: str


@dataclass(frozen=True)
class HumanWaitRequest:
    task: TaskSnapshot
    question: str
    resume_status: TaskStatus


@dataclass(frozen=True)
class HumanPrompt:
    task: TaskSnapshot
    question_id: str
    question_evidence_id: str
    question: str
    resume_status: TaskStatus


@dataclass(frozen=True)
class HumanAnswer:
    question_id: str
    wait_status: TaskStatus
    wait_version: int
    answer: str
    respondent: str


@dataclass(frozen=True)
class HumanAnswerRequest:
    prompt: HumanPrompt
    response: HumanAnswer


@dataclass(frozen=True)
class HumanAnswerResult:
    answer_evidence_id: str
    knowledge_draft_id: str


@dataclass(frozen=True)
class TransitionRequest:
    task: TaskSnapshot
    target: TaskStatus
    reason: str


@dataclass(frozen=True)
class WorkflowInput:
    task_id: str
    waits: list[TaskStatus] = field(default_factory=list)
    human_timeout_seconds: float = 3600
    activity_timeout_seconds: float = 30
    activity_max_attempts: int = 3
    investigation_json: str | None = None
    human_questions: list[HumanQuestion] = field(default_factory=list)
    verification_json: str | None = None
    execution_enabled: bool = False
    postmortem_enabled: bool = True
    ticket_id: str | None = None
    release_id: str | None = None
    release_observation_seconds: float = 60
    inspection_mode: str | None = None
    architecture_review: bool = False
    war_room: bool = False
    chat_mode: str | None = None


@dataclass(frozen=True)
class HumanResponse:
    wait_status: TaskStatus
    wait_version: int
    accepted: bool


@dataclass(frozen=True)
class ApprovalRequest:
    task: TaskSnapshot
    plan_evidence_id: str


@dataclass(frozen=True)
class ApprovalPrompt:
    task: TaskSnapshot
    approval_id: str
    request_evidence_id: str
    plan_evidence_id: str
    action_hash: str


@dataclass(frozen=True)
class ApprovalResponse:
    task_id: str
    approval_id: str
    wait_version: int
    action_hash: str
    decision: str
    actor: str


@dataclass(frozen=True)
class ApprovalDecisionRequest:
    prompt: ApprovalPrompt
    response: ApprovalResponse | None = None


@dataclass(frozen=True)
class ApprovalResult:
    evidence_id: str
    decision: str
    action_hash: str


@dataclass(frozen=True)
class WorkflowProgress:
    task: TaskSnapshot | None
    history: list[TaskSnapshot]
    conclusion_json: str | None = None
    conclusion_evidence_id: str | None = None
    review_evidence_id: str | None = None
    review_json: str | None = None
    action_plan_evidence_id: str | None = None
    action_plan_json: str | None = None
    human_prompt: HumanPrompt | None = None
    human_answers: list[HumanAnswerResult] = field(default_factory=list)
    approval_prompt: ApprovalPrompt | None = None
    approval_result: ApprovalResult | None = None
    verification_evidence_id: str | None = None
    verification_json: str | None = None
    execution_evidence_ids: list[str] = field(default_factory=list)
    safety_evidence_id: str | None = None
    takeover_notification_state: str | None = None
    postmortem_evidence_id: str | None = None
    postmortem_json: str | None = None
    legacy_wait: bool = False
