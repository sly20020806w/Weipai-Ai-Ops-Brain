"""Executor Activity；仅 Temporal 负责重试，写客户端与只读身份隔离。"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.kubernetes.execution import (
    FakeKubernetesWriteConnector,
    KubernetesWriteConnector,
)
from app.db.session import Database
from app.executor.models import ExecutionRequest, ExecutionResult
from app.executor.service import ExecutionStore


class ExecutorActivities:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        connector: KubernetesWriteConnector | None = None,
    ) -> None:
        if settings.app_env not in {"local", "test"} or settings.connector_mode.value != "fake":
            raise ValueError("当前 Executor Worker 仅允许本地 Fake；真实动作端待联调验收")
        self.connector = connector or FakeKubernetesWriteConnector()
        self.store = ExecutionStore(database, settings, self.connector)

    @activity.defn(name="executor.execute_action")
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        try:
            return await self.store.execute(request)
        except (ValueError, LookupError, PermissionError, TypeError) as error:
            raise ApplicationError(
                "执行授权或前提被拒绝", type=type(error).__name__, non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("执行结果未确认；保留意图并按相同幂等键核对") from None
