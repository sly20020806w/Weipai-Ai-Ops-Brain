"""L0 高级查询；Policy、Evidence、审计、Replay 均复用统一 Dispatcher。"""

from app.connectors.cloud.base import CloudConnector
from app.connectors.cloud.models import CloudQuery, CloudResources
from app.policy.models import RiskLevel
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class CloudResourcesInput(CloudQuery, ToolModel):
    pass


class CloudResourcesOutput(CloudResources, ToolModel):
    pass


def register_cloud_tools(registry: ToolRegistry, connector: CloudConnector) -> None:
    async def get_resources(query: CloudResourcesInput) -> CloudResourcesOutput:
        result = await connector.get_cloud_resources(CloudQuery.model_validate(query.model_dump()))
        if result.service_name != query.service_name or (
            query.start is not None and (result.start, result.end) != (query.start, query.end)
        ):
            raise ValueError("云资源响应与查询服务或时间窗不符")
        return CloudResourcesOutput.model_validate(result.model_dump())

    registry.register(
        name="get_cloud_resources",
        description="按服务读取关联云资源、RDS 连接数和 UTC 时间窗内云事件",
        input_model=CloudResourcesInput,
        output_model=CloudResourcesOutput,
        handler=get_resources,
        risk_level=RiskLevel.L0,
    )
