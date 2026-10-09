"""审批通知与决定 Activity；重试和超时由 Temporal 编排。"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.feishu.base import FeishuConnector
from app.connectors.feishu.factory import create_feishu_connector
from app.db.session import Database
from app.tasks.approval.service import ApprovalStore
from app.tasks.workflow_models import (
    ApprovalDecisionRequest,
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResult,
)


class ApprovalActivities:
    def __init__(
        self, database: Database, settings: Settings, *, connector: FeishuConnector | None = None
    ) -> None:
        self.store, self.settings, self.connector = (
            ApprovalStore(database, settings),
            settings,
            connector,
        )

    @activity.defn(name="approval.notify")
    async def notify(self, request: ApprovalRequest) -> ApprovalPrompt:
        connector = self.connector or create_feishu_connector(self.settings)
        try:
            return await self.store.notify(request, connector)
        except (ValueError, LookupError):
            raise ApplicationError("审批单被拒绝", non_retryable=True) from None
        except Exception:
            raise ApplicationError("审批通知失败") from None
        finally:
            if self.connector is None:
                await connector.aclose()

    @activity.defn(name="approval.decide")
    async def decide(self, request: ApprovalDecisionRequest) -> ApprovalResult:
        try:
            return await self.store.decide(request)
        except (ValueError, LookupError):
            raise ApplicationError("审批决定被拒绝", non_retryable=True) from None
        except Exception:
            raise ApplicationError("审批决定保存失败") from None
