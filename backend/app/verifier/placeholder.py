"""Step 17 历史 Activity 的兼容入口；现在执行显式 Fake 八项独立验证。"""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.activities import TaskActivityStore
from app.tasks.service import TaskNotFound
from app.tasks.states import TaskStatus
from app.tasks.workflow_models import TaskSnapshot, TransitionRequest
from app.tools.dispatcher import ToolDispatcher
from app.tools.verification_runtime import fake_verification_registry
from app.verifier.scenario import sample_spec
from app.verifier.service import VerificationService


class PlaceholderVerifier:
    def __init__(self, store: TaskActivityStore, *, app_env: str) -> None:
        if app_env not in {"local", "test"}:
            raise ValueError("占位 Verifier 仅允许 local/test")
        self.store = store
        self.settings = Settings(APP_ENV=app_env)

    @activity.defn(name="verifier.placeholder")
    async def verify(self, request: TransitionRequest) -> TaskSnapshot:
        if (
            request.task.status is not TaskStatus.VERIFYING
            or request.target is not TaskStatus.RESOLVED
        ):
            raise ApplicationError("占位 Verifier 仅允许 VERIFYING → RESOLVED", non_retryable=True)
        try:
            async with self.store.database.session() as session, session.begin():
                async with fake_verification_registry(self.settings, session) as registry:
                    result = await VerificationService(
                        session,
                        ToolDispatcher(
                            registry,
                            create_policy_engine(self.settings),
                            LedgerService(session),
                        ),
                        self.settings.verification_config,
                    ).verify(request.task, sample_spec(request.task), reason=request.reason)
                    return result.task
        except (TaskNotFound, ValueError) as error:
            raise ApplicationError(
                "占位验证状态迁移被拒绝", type=type(error).__name__, non_retryable=True
            ) from None
