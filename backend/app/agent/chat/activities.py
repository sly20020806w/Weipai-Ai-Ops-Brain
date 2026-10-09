"""已提交输入重投去重；错误只报告可公开的业务类型。"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.chat.models import ChatSubmission
from app.agent.chat.service import submit_chat
from app.db.session import Database
from app.tasks.console_queries import ConsoleConflict, ConsoleNotFound
from app.triggers.schemas import EventReceipt


class ChatActivities:
    def __init__(self, database: Database) -> None:
        self.database = database

    @activity.defn(name="chat.persist")
    async def persist(self, submission_json: str) -> EventReceipt:
        try:
            value = ChatSubmission.model_validate_json(submission_json)
            async with self.database.session() as session, session.begin():
                return await submit_chat(session, value)
        except ConsoleNotFound:
            raise ApplicationError(
                "上一轮对话不存在", type="ChatNotFound", non_retryable=True
            ) from None
        except ConsoleConflict:
            raise ApplicationError(
                "对话请求身份或内容冲突", type="ChatConflict", non_retryable=True
            ) from None
        except ValueError:
            raise ApplicationError("对话输入无效", type="ChatInvalid", non_retryable=True) from None
