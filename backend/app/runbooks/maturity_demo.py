"""独立临时库中的默认阈值成熟度演示；所有事实来自 Fake 和独立 Verifier。"""

import asyncio
import os
from uuid import uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import Settings
from app.db.session import Database
from app.policy.engine import PolicyEngine
from app.policy.models import (
    PolicyAction,
    PolicyConfig,
    PolicyDecision,
    PolicyEnvironment,
    PolicyRule,
    RiskLevel,
)
from app.runbooks.embedding import embedding_client
from app.runbooks.lifecycle import RunbookLifecycle
from app.runbooks.maturity import MaturityConfig
from app.runbooks.maturity_scenario import create_sample, human_review, prepare_trial
from app.runbooks.schemas import RunbookMaturity
from app.runbooks.service import RunbookService
from app.tasks.states import TaskStatus
from app.tasks.worker import create_worker, start_task_workflow
from app.tasks.workflow import AITaskWorkflow
from app.tasks.workflow_models import WorkflowInput
from app.tools.verification_runtime import fake_verification_registry
from app.verifier.activities import VerifierActivities
from app.verifier.models import VerificationRequest
from app.verifier.scenario import sample_spec


async def run_demo(url: URL) -> None:
    if url.host != "127.0.0.1" or not (url.database or "").startswith("weipai_db_test_"):
        raise ValueError("成熟度演示只允许本机独立临时库")
    config = Settings(
        APP_ENV="test",
        RUNBOOK_MATURITY_CONFIG=MaturityConfig(),
        TEMPORAL_CONFIG={
            "address": os.environ["TEST_TEMPORAL_ADDRESS"],
            "task_queue": f"maturity-demo-{uuid4().hex}",
        },
    )
    database = Database(url)
    try:
        guide = await create_sample(database, config)
        print(f"Runbook {guide.id}：Draft，成功 0／失败 0", flush=True)
        guide = await human_review(database, config, guide)
        print("人工审核已留操作人和 Evidence：Draft → Reviewed", flush=True)
        verifier = VerifierActivities(database, config, registry_factory=fake_verification_registry)
        policy = PolicyEngine(
            environment=PolicyEnvironment(config.app_env),
            config=PolicyConfig(
                rules=(
                    PolicyRule(
                        id="fake-explicit-allow",
                        risk_levels=tuple(RiskLevel),
                        decision=PolicyDecision.ALLOW,
                        reason="Fake 验收明确规则",
                    ),
                )
            ),
        )
        levels = {3: "Verified", 5: "Semi-Automated", 10: "Approval-Automated", 20: "Self-Healing"}
        for count in range(1, 21):
            trial = await prepare_trial(database, config, guide.id)
            request = VerificationRequest(trial, sample_spec(trial).model_dump_json())
            if count == 20:
                client = await Client.connect(config.temporal_config.address)
                async with create_worker(client, database, config, verifier_activities=verifier):
                    handle = await start_task_workflow(
                        client,
                        WorkflowInput(
                            trial.task_id,
                            verification_json=request.spec_json,
                            postmortem_enabled=False,
                        ),
                        task_queue=config.temporal_config.task_queue,
                    )
                    result = await asyncio.wait_for(handle.result(), 30)
                assert result.task and result.task.status is TaskStatus.RESOLVED
                await Replayer(workflows=[AITaskWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
                print(f"Temporal Workflow：{handle.id}，历史回放通过", flush=True)
            verified = await verifier.verify(request)
            assert verified.task.status is TaskStatus.RESOLVED
            async with database.session() as session, session.begin():
                guide = await RunbookService(session, lambda r: embedding_client(config, r)).get(
                    guide.id
                )
                context = await RunbookLifecycle(session).context(guide)
                low = policy.evaluate(
                    PolicyAction(name="fake_low_risk", risk_level=RiskLevel.L1, runbook=context)
                )
                high = policy.evaluate(
                    PolicyAction(name="rollback_prod", risk_level=RiskLevel.L3, runbook=context)
                )
                assert guide.success_count == count
                assert high.decision is PolicyDecision.NEED_APPROVAL
                assert low.decision is (
                    PolicyDecision.ALLOW if count == 20 else PolicyDecision.NEED_APPROVAL
                )
            if count in levels:
                print(
                    f"{count} 次独立验证成功 → {levels[count]}；可信度 {guide.confidence:.4f}；"
                    f"验证 Evidence {verified.evidence_id}",
                    flush=True,
                )
        assert guide.maturity is RunbookMaturity.SELF_HEALING
        print(
            "仅经明确 Policy allow 的 L1/L2 可自动允许；L3 回滚仍需审批。重投没有重复计数。",
            flush=True,
        )
        failed = VerifierActivities(
            database,
            config,
            registry_factory=lambda s, db: fake_verification_registry(s, db, recovered=False),
        )
        for count in (1, 2):
            trial = await prepare_trial(database, config, guide.id)
            await failed.verify(VerificationRequest(trial, sample_spec(trial).model_dump_json()))
            async with database.session() as session:
                guide = await RunbookService(session, lambda r: embedding_client(config, r)).get(
                    guide.id
                )
            print(
                f"失败 {count} 次 → {guide.maturity.value}；"
                f"成功 {guide.success_count}／失败 {guide.failure_count}",
                flush=True,
            )
        assert guide.maturity is RunbookMaturity.REVIEWED and guide.failure_count == 2
        print("连续失败降级且撤销审核，需要重新审核。实际运维动作执行次数：0。", flush=True)
        print("Step 35 Runbook 成熟度 Fake 演示全部通过", flush=True)
    finally:
        await database.dispose()
