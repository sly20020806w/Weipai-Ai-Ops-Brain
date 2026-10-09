"""独立 Temporal 验证 Activity；重试/生命周期全部交给 Temporal。"""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.connectors.kubernetes.execution import ExecutionReceipt
from app.db.session import Database
from app.executor.models import ExecutionCommand
from app.executor.verification import require_execution_target
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.models import AITask
from app.tasks.states import TaskStatus
from app.tasks.workflow_models import TaskSnapshot
from app.tools.dispatcher import ToolDispatcher
from app.tools.registry import ToolRegistry
from app.tools.verification_runtime import verification_registry
from app.verifier.models import VerificationRequest, VerificationResult, VerificationSpec
from app.verifier.service import VerificationService

RegistryFactory = Callable[[Settings, AsyncSession], AbstractAsyncContextManager[ToolRegistry]]


class VerifierActivities:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        registry_factory: RegistryFactory | None = None,
    ) -> None:
        self.database, self.settings = database, settings
        self.registry_factory = registry_factory or verification_registry

    @activity.defn(name="verifier.prepare_after_execution")
    async def prepare(self, snapshot: TaskSnapshot) -> str:
        try:
            async with self.database.session() as session, session.begin():
                task = await session.scalar(
                    select(AITask).where(AITask.id == UUID(snapshot.task_id)).with_for_update()
                )
                if (
                    task is None
                    or snapshot.status is not TaskStatus.VERIFYING
                    or (task.status, task.status_version) != (snapshot.status, snapshot.version)
                ):
                    raise ValueError("执行后验证必须绑定当前 VERIFYING 版本")
                ledger = LedgerService(session)
                records = await ledger.evidence_for_task(task.id)
                completed = []
                for entry in records:
                    phase = entry.parameters.get("task")
                    if (
                        entry.source_tool == "execution.complete"
                        and isinstance(phase, dict)
                        and phase.get("version") == snapshot.version - 1
                    ):
                        completed.append(entry)
                if len(completed) != 1 or not isinstance(completed[0].result_snapshot, dict):
                    raise ValueError("缺少唯一执行完成检查点")
                ids = completed[0].result_snapshot.get("evidence_ids")
                if not isinstance(ids, list) or not ids or not isinstance(ids[-1], str):
                    raise ValueError("缺少执行回执")
                record = await ledger.get_evidence(UUID(ids[-1]))
                command = ExecutionCommand.model_validate_json(json.dumps(record.parameters))
                receipt = ExecutionReceipt.model_validate_json(json.dumps(record.result_snapshot))
                target = receipt.target
                resources = self.settings.verification_config.resources_by_service.get(
                    target.service_name
                )
                if not resources:
                    raise ValueError("缺少宿主配置的资源恢复标准")
                spec = VerificationSpec(
                    task_id=task.id,
                    verifying_version=snapshot.version,
                    action_id=command.action_id,
                    action_completed_at=receipt.completed_at,
                    service_name=target.service_name,
                    start=receipt.completed_at,
                    end=receipt.completed_at
                    + timedelta(seconds=self.settings.verification_config.window_seconds),
                    cluster_name=target.cluster_name,
                    namespace=target.namespace,
                    deployment_name=target.deployment_name,
                    container_name=target.container_name,
                    expected_image=target.image,
                    expected_replicas=target.replicas,
                    resources=resources,
                )
                await require_execution_target(session, spec)
                return spec.model_dump_json()
        except (ValueError, LookupError, TypeError):
            raise ApplicationError(
                "无法确定独立验证目标或资源恢复标准", non_retryable=True
            ) from None

    @activity.defn(name="verifier.verify_action")
    async def verify(self, request: VerificationRequest) -> VerificationResult:
        try:
            spec = VerificationSpec.model_validate_json(request.spec_json)
            async with self.database.session() as session, session.begin():
                async with self.registry_factory(self.settings, session) as registry:
                    dispatcher = ToolDispatcher(
                        registry, create_policy_engine(self.settings), LedgerService(session)
                    )
                    return await VerificationService(
                        session,
                        dispatcher,
                        self.settings.verification_config,
                        self.settings.safety_config,
                        self.settings.runbook_maturity_config,
                    ).verify(request.task, spec)
        except (ValueError, LookupError, TypeError) as error:
            raise ApplicationError(
                "独立验证请求被拒绝", type=type(error).__name__, non_retryable=True
            ) from None
