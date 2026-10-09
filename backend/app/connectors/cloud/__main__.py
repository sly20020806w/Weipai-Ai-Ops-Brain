"""Step 15 可直接运行的确定性 Fake 样例；无需云凭证或数据库。"""

import asyncio
import json

from app.connectors.cloud.fake import SAMPLE_END, SAMPLE_START, FakeCloudConnector
from app.connectors.cloud.models import CloudQuery
from app.policy.models import RiskLevel
from app.tools.cloud import register_cloud_tools
from app.tools.registry import ToolRegistry


async def main() -> None:
    # 显式构造 Fake，不读取宿主 CLOUD_CONFIG 或真实 Reader 凭证。
    async with FakeCloudConnector() as reader:
        registry = ToolRegistry()
        register_cloud_tools(registry, reader)
        result = await reader.get_cloud_resources(
            CloudQuery(
                service_name="payment-service",
                start=SAMPLE_START,
                end=SAMPLE_END,
            )
        )
        assert len(result.resources) == 8 and len(result.events) == 1
        observation = next(r for r in result.resources if r.product == "rds").rds_connections
        assert observation is not None and observation.total_connections == 520
        assert all(item.risk_level is RiskLevel.L0 for item in registry.declarations())
        print(
            json.dumps(
                {
                    "mode": "fake",
                    "tools": [d.name for d in registry.declarations()],
                    "result": result.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    print("Step 15 Fake 样例验收通过（Dispatcher Evidence/审计见专项测试，未连接真实系统）")


if __name__ == "__main__":
    asyncio.run(main())
