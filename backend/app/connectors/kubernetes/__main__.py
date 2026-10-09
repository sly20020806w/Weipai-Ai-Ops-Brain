"""不需要凭证和数据库的 Fake 样例，Tool 证据/审计由专项测试验收。"""

import asyncio
import json

from app.config import Settings
from app.connectors.kubernetes.factory import create_kubernetes_connector


async def main() -> None:
    settings = Settings(
        APP_ENV="local",
        CONNECTOR_MODE="fake",
        CONNECTOR_READER_TOKENS={},
        KUBERNETES_CONFIG=None,
        OPS_PLATFORM_CONFIG=None,
        PROMETHEUS_CONFIG=None,
        SLS_CONFIG=None,
        ARMS_CONFIG=None,
        DATABASE_URL=None,
        LLM_MODE="fake",
    )
    async with create_kubernetes_connector(settings) as connector:
        deployments = await connector.list_deployments("payment", service_name="payment-service")
        pods = await connector.list_pods("payment", service_name="payment-service")
        events = await connector.list_events("payment", service_name="payment-service")
        assert len(deployments) == 1 and deployments[0].status.ready_replicas == 2
        assert len(pods) == 3 and pods[2].status.container_statuses[0].restart_count == 4
        assert len(events) == 1 and events[0].reason == "BackOff"
        print(
            json.dumps(
                {
                    "mode": "fake",
                    "cluster_name": connector.cluster_name,
                    "deployments": [item.model_dump(mode="json") for item in deployments],
                    "pods": [item.model_dump(mode="json") for item in pods],
                    "events": [item.model_dump(mode="json") for item in events],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    print("Step 12 Fake 样例验收通过（未连接真实集群）")


if __name__ == "__main__":
    asyncio.run(main())
