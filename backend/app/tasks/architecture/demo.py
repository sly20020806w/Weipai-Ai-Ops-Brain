"""本机临时库与隔离 Temporal Worker：单点数据库十二维评审演示。"""

import asyncio
import json
import os
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy.engine import URL
from temporalio.client import Client
from temporalio.worker import Replayer

from app.agent.models import EmbeddingRequest
from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.graph.service import GraphService
from app.knowledge.schemas import KnowledgeDraft, KnowledgeType
from app.knowledge.service import KnowledgeService
from app.ledger.models import AuditEventType
from app.ledger.service import LedgerService
from app.runbooks.embedding import embedding_client
from app.tasks.architecture.models import ArchitectureReport, ReviewSubmission
from app.tasks.architecture.scenario import SAMPLE_PROPOSAL, SAMPLE_STANDARD
from app.tasks.architecture.service import submit_review
from app.tasks.service import TaskService
from app.tasks.states import TaskStatus
from app.tasks.worker import create_worker
from app.tasks.workflow import AITaskWorkflow
from app.triggers.activities import EventActivities


async def seed_sample(database: Database) -> None:
    settings = Settings(APP_ENV="test")
    request = EmbeddingRequest(inputs=(SAMPLE_STANDARD,))
    llm = embedding_client(settings, request)
    try:
        async with database.session() as session, session.begin():
            graph = GraphService(session)
            service = await graph.upsert_node(
                kind="service", external_id="payment-service", name="支付服务"
            )
            db = await graph.upsert_node(
                kind="database", external_id="payment-db", name="支付数据库"
            )
            await graph.upsert_edge(
                from_node_id=service.id,
                to_node_id=db.id,
                relation="uses",
                source="fake-architecture-sample",
                confidence=0.99,
                observed_at=utc_now(),
            )
            await KnowledgeService(session, llm).create(
                KnowledgeDraft(
                    kind=KnowledgeType.STANDARD,
                    content=SAMPLE_STANDARD,
                    source="Fake 公司规范样例",
                    valid_from=utc_now() - timedelta(days=1),
                )
            )
    finally:
        await llm.aclose()


async def run_demo(url: URL) -> None:
    address = os.environ.get("TEST_TEMPORAL_ADDRESS", "")
    if (
        url.host != "127.0.0.1"
        or not (url.database or "").startswith("weipai_db_test_")
        or address.rpartition(":")[0] != "127.0.0.1"
    ):
        raise ValueError("架构评审演示只允许本机临时库和 Temporal")
    database = Database(url)
    client = await Client.connect(
        address, namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "default")
    )
    queue = f"architecture-demo-{uuid4().hex}"
    settings = Settings(APP_ENV="test", TEMPORAL_CONFIG={"task_queue": queue, "address": address})
    try:
        await seed_sample(database)
        value = ReviewSubmission(
            request_id=uuid4(),
            service_name="payment-service",
            title="支付数据库方案",
            proposal=SAMPLE_PROPOSAL,
        )
        async with database.session() as session, session.begin():
            receipt = await submit_review(session, value)
            repeated = await submit_review(session, value)
            assert repeated.duplicate and repeated.task_id == receipt.task_id
        async with create_worker(client, database, settings):
            await EventActivities(database, settings, client).start_task(receipt)
            handle = client.get_workflow_handle_for(AITaskWorkflow.run, receipt.workflow_id)
            try:
                progress = await asyncio.wait_for(handle.result(), timeout=60)
            except BaseException:
                await handle.terminate("清理未完成的本机架构评审演示")
                raise
            assert progress.task and progress.task.status is TaskStatus.CLOSED
            report = ArchitectureReport.model_validate_json(progress.conclusion_json or "{}")
            assert len(report.dimensions) == 12
            assert all(d.outcome == "risk" for d in report.dimensions[:2])
            assert all(
                any(c.evidence_id == report.sources.standards for c in d.citations)
                for d in report.dimensions[:2]
            )
            await Replayer(workflows=[AITaskWorkflow]).replay_workflow(await handle.fetch_history())
        async with database.session() as session:
            ledger = LedgerService(session)
            audits = await ledger.audits_for_task(UUID(receipt.task_id))
            history = await TaskService(session).history(UUID(receipt.task_id))
            assert [h.to_status for h in history] == [h.status for h in progress.history]
            assert not any(a.event_type is AuditEventType.EXECUTION for a in audits)
            for dimension in report.dimensions:
                print(
                    f"{dimension.dimension}：{dimension.outcome} — {dimension.finding}", flush=True
                )
                for citation in dimension.citations:
                    evidence = await ledger.get_evidence(citation.evidence_id)
                    print(
                        f"  Evidence {evidence.id} ({evidence.source_tool})：{citation.quote}",
                        flush=True,
                    )
        print(
            json.dumps(
                {
                    "评审维度数": 12,
                    "单点风险维度": [d.dimension for d in report.dimensions if d.outcome == "risk"],
                    "待补充维度数": sum(d.outcome == "unknown" for d in report.dimensions),
                    "重复提交新增任务": 0,
                    "实际运维动作数": 0,
                    "Temporal 历史回放": "通过",
                    "任务状态": "CLOSED",
                    "Workflow ID": receipt.workflow_id,
                    "报告 Evidence ID": progress.conclusion_evidence_id,
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
    finally:
        await database.dispose()
    print("Step 41 架构评审 Fake 演示全部通过", flush=True)
