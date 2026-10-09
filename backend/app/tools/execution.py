"""写入实现只在 Dispatcher 的可信执行范围内签发动作专属凭证。"""

from app.connectors.kubernetes.execution import ExecutionReceipt, KubernetesWriteConnector
from app.executor.models import ExecutionCommand, ExecutionTarget, TargetQuery
from app.policy.models import RiskLevel
from app.tools.registry import ToolRegistry


def register_execution_tools(
    registry: ToolRegistry, connector: KubernetesWriteConnector, ttl_seconds: int
) -> None:
    async def inspect(query: TargetQuery) -> ExecutionTarget:
        return await connector.inspect(query)

    async def execute(command: ExecutionCommand) -> ExecutionReceipt:
        credential = await connector.issue(command, ttl_seconds)
        return await connector.execute(command, credential)

    registry.register(
        name="get_execution_target",
        description="读取精确 Deployment UID、资源版本与操作前状态",
        input_model=TargetQuery,
        output_model=ExecutionTarget,
        handler=inspect,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="execute_action",
        description="执行宿主已验证 Policy 或精确审批的动作；默认关闭",
        input_model=ExecutionCommand,
        output_model=ExecutionReceipt,
        handler=execute,
        risk_level=RiskLevel.L5,
    )
