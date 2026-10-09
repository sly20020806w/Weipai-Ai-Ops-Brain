"""直接运行离线样例；Dispatcher Evidence/审计由同一专项脚本的测试验证。"""

import asyncio
import json
from contextlib import AsyncExitStack

from app.config import Settings
from app.connectors.observability.factory import (
    create_arms_connector,
    create_prometheus_connector,
    create_sls_connector,
)
from app.connectors.observability.fake import SAMPLE_END, SAMPLE_START
from app.connectors.observability.models import MetricsQuery, Window, topology


async def main() -> None:
    settings = Settings(
        APP_ENV="local",
        CONNECTOR_MODE="fake",
        CONNECTOR_READER_TOKENS={},
        PROMETHEUS_CONFIG=None,
        SLS_CONFIG=None,
        ARMS_CONFIG=None,
        KUBERNETES_CONFIG=None,
        OPS_PLATFORM_CONFIG=None,
        DATABASE_URL=None,
        LLM_MODE="fake",
    )
    query = Window(service_name="payment-service", start=SAMPLE_START, end=SAMPLE_END)
    async with AsyncExitStack() as stack:
        prom = await stack.enter_async_context(create_prometheus_connector(settings))
        sls = await stack.enter_async_context(create_sls_connector(settings))
        arms = await stack.enter_async_context(create_arms_connector(settings))
        metrics = await prom.query_metrics(MetricsQuery(**query.model_dump()))
        logs = await sls.query_logs(query)
        traces = await arms.query_traces(query)
        edges = topology(traces)
        assert len(metrics) == 1 and len(metrics[0].points) == len(logs) == len(traces) == 2
        assert len(edges) == 2 and all(edge.target_service == "payment-db" for edge in edges)
        print(
            json.dumps(
                {
                    "mode": "fake",
                    "window": query.model_dump(mode="json"),
                    "metrics": [item.model_dump(mode="json") for item in metrics],
                    "logs": [item.model_dump(mode="json") for item in logs],
                    "traces": [item.model_dump(mode="json") for item in traces],
                    "topology": [item.model_dump(mode="json") for item in edges],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    print("Step 13 Fake 样例验收通过（未连接真实系统）")


if __name__ == "__main__":
    asyncio.run(main())
