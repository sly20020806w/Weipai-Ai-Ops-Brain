"""本地 Fake 人工验收；使用正在运行的同镜像 Worker。"""

import argparse
import asyncio
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from temporalio.client import Client

from app.config import Settings
from app.db.session import Database
from app.graph.discovery.models import DiscoveryResult
from app.graph.discovery.schedule import discovery_input
from app.graph.discovery.workflow import DiscoveryWorkflow
from app.graph.models import GraphEdge
from app.graph.service import GraphService
from app.ledger.service import LedgerService
from app.policy.engine import create_policy_engine
from app.tasks.service import TaskService
from app.tasks.states import TaskSource
from app.tasks.worker import validate_placeholder_settings
from app.tools import DispatchMode, DispatchStatus, ToolDispatcher
from app.tools.graph import GraphContext, register_graph_tools
from app.tools.registry import ToolRegistry


async def run_demo(mode: str) -> None:
    settings = Settings()
    validate_placeholder_settings(settings)
    client = await Client.connect(
        settings.temporal_config.address, namespace=settings.temporal_config.namespace
    )
    schedule = client.get_schedule_handle(settings.discovery_config.schedule_id)
    if mode != "once":
        if mode == "pause":
            await schedule.pause(note="人工结束 Discovery 周期验收")
        elif mode == "resume":
            await schedule.unpause(note="人工恢复 Discovery 周期验收")
        description = await schedule.describe()
        print(
            f"Schedule：{schedule.id}；paused={description.schedule.state.paused}；"
            f"周期={description.schedule.spec.intervals[0].every}；"
            f"已触发={description.info.num_actions} 次",
            flush=True,
        )
        for scheduled_result in description.info.recent_actions[-5:]:
            print(
                f"周期执行：{scheduled_result.started_at.isoformat()}，{scheduled_result.action}",
                flush=True,
            )
        return
    database = Database(settings.require_database_url())
    try:
        previous = None
        for index in (1, 2):
            handle = await client.start_workflow(
                DiscoveryWorkflow.run,
                discovery_input(settings.discovery_config),
                id=f"discovery-demo-{uuid4().hex}",
                task_queue=settings.temporal_config.task_queue,
            )
            print(f"第 {index} 次 Discovery Workflow ID：{handle.id}", flush=True)
            result: DiscoveryResult = await asyncio.wait_for(handle.result(), timeout=60)
            async with database.session() as session:
                edges = {
                    edge.id: edge.last_seen for edge in await session.scalars(select(GraphEdge))
                }
            print(
                f"本轮发现 {result.node_count} 个节点、{result.edge_count} 条关系；"
                f"未配置绑定：{', '.join(result.missing_bindings) or '无'}",
                flush=True,
            )
            if previous is not None:
                assert previous.keys() == edges.keys(), "重复发现产生不同的关系 ID"
                assert all(edges[key] >= timestamp for key, timestamp in previous.items())
                assert any(edges[key] > timestamp for key, timestamp in previous.items())
                print("重复发现通过：关系 ID 不变、last_seen 已刷新", flush=True)
            previous = edges
        async with database.session() as session, session.begin():
            task = await TaskService(session).create(
                source=TaskSource.HUMAN,
                title="Discovery 查询验收（Fake）",
                reason="Step 18 人工验收",
            )
            ledger = LedgerService(session)
            registry = ToolRegistry()
            register_graph_tools(registry, GraphService(session))
            dispatcher = ToolDispatcher(registry, create_policy_engine(settings), ledger)
            context = await dispatcher.dispatch(
                task_id=task.id,
                tool_name="get_service_context",
                parameters={"service_name": "payment-service"},
                actor="local-demo",
            )
            dependencies = await dispatcher.dispatch(
                task_id=task.id,
                tool_name="get_dependencies",
                parameters={"service_name": "payment-service"},
                actor="local-demo",
            )
            for value in (context, dependencies):
                assert value.status is DispatchStatus.SUCCEEDED and value.evidence_id is not None
                print(f"L0 Tool 成功，Evidence ID：{value.evidence_id}", flush=True)
            assert context.result is not None and dependencies.result is not None
            context_view = GraphContext.model_validate_json(json.dumps(context.result))
            dependency_view = GraphContext.model_validate_json(json.dumps(dependencies.result))
            assert {n.kind for n in context_view.nodes} >= {
                "repository",
                "version",
                "cluster",
                "pod",
                "database",
                "cache",
                "topic",
            }
            print(
                "服务上下文节点：" + "，".join(f"{n.name}({n.kind})" for n in context_view.nodes),
                flush=True,
            )
            print(
                f"上下文关系：{len(context_view.edges)} 条，"
                f"读取时间 {context_view.as_of.isoformat()}",
                flush=True,
            )
            names = {n.id: n.external_id for n in dependency_view.nodes}
            for edge in dependency_view.edges:
                print(
                    f"调用：{names[edge.from_node_id]} → {names[edge.to_node_id]}；"
                    f"source={edge.source}；confidence={edge.confidence}；"
                    f"first_seen={edge.first_seen.isoformat()}；last_seen={edge.last_seen.isoformat()}；"
                    f"freshness_seconds={edge.freshness_seconds:.3f}",
                    flush=True,
                )
            assert len(await ledger.evidence_for_task(task.id)) == 2
            assert context.evidence_id is not None
            evidence = await ledger.get_evidence(context.evidence_id)
            replay = await dispatcher.dispatch(
                task_id=task.id,
                tool_name="get_service_context",
                parameters={"service_name": "payment-service"},
                actor="local-replay",
                mode=DispatchMode.REPLAY,
                replay_evidence_id=evidence.id,
                replay_before=evidence.collected_at + timedelta(seconds=1),
            )
            assert replay.result == context.result and replay.status is DispatchStatus.REPLAYED
            print("Replay 通过：返回原 Evidence ID 和原始 freshness 快照", flush=True)
        print("Step 18 演示通过；可在 Temporal UI 查看两条 DiscoveryWorkflow", flush=True)
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 18 Fake Discovery 演示")
    parser.add_argument(
        "mode", nargs="?", default="once", choices=("once", "schedule", "pause", "resume")
    )
    asyncio.run(run_demo(parser.parse_args().mode))


if __name__ == "__main__":
    main()
