"""六类 Fake 已采集证据演示，独立临时库，不操作真实系统。"""

from uuid import uuid4

from sqlalchemy.engine import URL

from app.config import Settings
from app.connectors.feishu.fake import FakeFeishuConnector
from app.connectors.kubernetes.execution import FakeKubernetesWriteConnector
from app.db.session import Database
from app.executor.models import ExecutionRequest
from app.executor.service import ExecutionStore
from app.tasks.safety.activities import SafetyActivities
from app.tasks.safety.models import REASON_TEXT, AbortReason, AutomationAborted
from app.tasks.safety.scenario import seed_case
from app.tasks.service import TaskService
from app.tasks.states import TaskSource, TaskStatus
from app.tasks.workflow_models import TaskSnapshot


async def run_demo(url: URL) -> None:
    if url.host != "127.0.0.1" or not (url.database or "").startswith("weipai_db_test_"):
        raise ValueError("熔断演示只允许本机隔离临时库")
    database = Database(url)
    config = Settings(APP_ENV="test", EXECUTION_CONFIG={"enabled": True})
    try:
        for reason in AbortReason:
            async with database.session() as session, session.begin():
                tasks = TaskService(session)
                task = await tasks.create(
                    source=TaskSource.ALERT,
                    title=f"熔断演示：{REASON_TEXT[reason]}",
                    reason="Step 33 Fake 验收",
                )
                for status in (
                    TaskStatus.CONTEXT_BUILDING,
                    TaskStatus.RUNBOOK_MATCHING,
                    TaskStatus.INVESTIGATING,
                ):
                    task = await tasks.transition(
                        task.id,
                        status,
                        expected_status=task.status,
                        expected_version=task.status_version,
                        reason="准备 Fake 调查事实",
                    )
                await seed_case(session, task.id, reason)
                snapshot = TaskSnapshot(str(task.id), task.status, task.status_version)
            write, feishu = FakeKubernetesWriteConnector(), FakeFeishuConnector()
            safety = SafetyActivities(database, config, connector=feishu)
            result = await safety.check(snapshot)
            assert (
                result.task.status is TaskStatus.AUTOMATION_ABORTED
                and reason.value in result.reasons
            )
            await safety.notify(snapshot.task_id)
            try:
                await ExecutionStore(database, config, write).execute(
                    ExecutionRequest(snapshot, str(uuid4()))
                )
            except AutomationAborted:
                pass
            else:
                raise AssertionError("熔断后仍能自动执行")
            assert write.issue_count == write.execution_count == 0
            assert len(feishu.sent_messages) == 1
            assert await safety.check(snapshot) == result
            print(f"\n{REASON_TEXT[reason]}：AUTOMATION_ABORTED", flush=True)
            print(
                f"任务 ID：{snapshot.task_id}；熔断 Evidence ID：{result.evidence_id}", flush=True
            )
            print("后续动作：拒绝；凭证签发/实际 Fake 运维执行次数：0/0；接管通知：1", flush=True)
            print(feishu.sent_messages[0].notification.model_dump_json(), flush=True)
        print("\nStep 33 六种熔断 Fake 演示全部通过", flush=True)
    finally:
        await database.dispose()
