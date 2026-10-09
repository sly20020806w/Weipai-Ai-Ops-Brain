"""Temporal 管理重试与生命周期，Activity 管理事务和网关连接。"""

from collections.abc import Callable

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.client import LLMClient
from app.agent.fake import create_llm_client
from app.config import Settings
from app.db.session import Database
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.architecture.models import ArchitectureRequest, ArchitectureResult
from app.tasks.architecture.scenario import fake_review_llm
from app.tasks.architecture.service import ArchitectureService
from app.tools.architecture import architecture_registry
from app.tools.dispatcher import ToolDispatcher


class ArchitectureActivities:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        llm_factory: Callable[[], LLMClient] | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.llm_factory = llm_factory or (
            fake_review_llm if settings.llm_mode == "fake" else lambda: create_llm_client(settings)
        )

    @activity.defn(name="architecture.review")
    async def review(self, request: ArchitectureRequest) -> ArchitectureResult:
        llm = self.llm_factory()
        try:
            # 先提交真实查询及其审计，后生成报告；无效模型响应不能抹掉已经发生的 Tool 调用。
            async with self.database.session() as session, session.begin():
                service = ArchitectureService(
                    session,
                    ToolDispatcher(
                        architecture_registry(session, self.settings),
                        create_policy_engine(self.settings),
                        LedgerService(session),
                    ),
                    llm,
                )
                saved = await service.collect(request)
            if saved is not None:
                return saved
            async with self.database.session() as session, session.begin():
                service = ArchitectureService(
                    session,
                    ToolDispatcher(
                        architecture_registry(session, self.settings),
                        create_policy_engine(self.settings),
                        LedgerService(session),
                    ),
                    llm,
                )
                return await service.review(request)
        except (ValueError, LookupError, TypeError):
            raise ApplicationError("架构评审输入、阶段或证据被拒绝", non_retryable=True) from None
        finally:
            await llm.aclose()
