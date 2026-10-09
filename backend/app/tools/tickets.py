"""权限事实与工单执行高级 Tool，调用仅经 Dispatcher。"""

from app.connectors.ops_platform.tickets import TicketReader, TicketWriteConnector
from app.executor.ticket_models import (
    PermissionQuery,
    PermissionState,
    TicketCommand,
    TicketReceipt,
)
from app.policy.models import RiskLevel
from app.tools.registry import ToolRegistry


def register_ticket_reads(registry: ToolRegistry, reader: TicketReader) -> None:
    registry.register(
        name="query_ticket_permission",
        description="独立读取服务、用户、资源的当前权限",
        input_model=PermissionQuery,
        output_model=PermissionState,
        handler=reader.get_permission,
        risk_level=RiskLevel.L0,
    )


def register_ticket_execution(
    registry: ToolRegistry, writer: TicketWriteConnector, ttl: int
) -> None:
    async def execute(command: TicketCommand) -> TicketReceipt:
        return await writer.execute(command, await writer.issue(command, ttl))

    registry.register(
        name="execute_action",
        description="执行精确授权的权限处理或工单回填关闭",
        input_model=TicketCommand,
        output_model=TicketReceipt,
        handler=execute,
        risk_level=RiskLevel.L5,
    )
