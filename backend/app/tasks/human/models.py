"""人工问答的纯数据契约，等待身份绑定任务、状态与版本。"""

from uuid import UUID, uuid5

from app.tasks.states import TaskStatus
from app.tasks.workflow_models import HumanAnswer, HumanQuestion, TaskSnapshot

HUMAN_WAIT_STATUSES = frozenset({TaskStatus.NEED_HUMAN_JUDGMENT, TaskStatus.WAITING_INFORMATION})


def question_id(task: TaskSnapshot) -> str:
    return str(uuid5(UUID(task.task_id), f"human-question:{task.status.value}:{task.version}"))


def validate_question(value: HumanQuestion) -> None:
    if (
        not isinstance(value.wait_status, TaskStatus)
        or value.wait_status not in HUMAN_WAIT_STATUSES
    ):
        raise ValueError("人工问答只允许判断与补充信息，不能用作审批")
    if (
        not isinstance(value.question, str)
        or not value.question.strip()
        or not 1 <= len(value.question) <= 3000
    ):
        raise ValueError("问题必须为 1–3000 字的非空正文")


def validate_answer(value: HumanAnswer) -> None:
    if str(UUID(value.question_id)) != value.question_id:
        raise ValueError("问题 ID 必须是规范 UUID")
    if (
        not isinstance(value.wait_status, TaskStatus)
        or value.wait_status not in HUMAN_WAIT_STATUSES
    ):
        raise ValueError("人工回答不能授权动作")
    if type(value.wait_version) is not int or value.wait_version < 1:
        raise ValueError("等待版本必须为正整数")
    if (
        not isinstance(value.answer, str)
        or not value.answer.strip()
        or not 1 <= len(value.answer) <= 8000
    ):
        raise ValueError("回答必须为 1–8000 字的非空正文")
    if (
        not isinstance(value.respondent, str)
        or not value.respondent.strip()
        or not 1 <= len(value.respondent) <= 200
    ):
        raise ValueError("回答必须记录操作人")
