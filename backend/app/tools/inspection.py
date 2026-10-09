"""巡检事实为 L0；扫描与 Replay 只通过唯一 Dispatcher。"""

from app.connectors.inspection.client import InspectionConnector
from app.connectors.inspection.models import InspectionFacts, InspectionQuery
from app.policy.models import RiskLevel
from app.tools.registry import ToolRegistry


def register_inspection_tools(registry: ToolRegistry, connector: InspectionConnector) -> None:
    registry.register(
        name="query_inspection_facts",
        description="读取服务巡检和治理事实及来源引用",
        input_model=InspectionQuery,
        output_model=InspectionFacts,
        risk_level=RiskLevel.L0,
        handler=connector.query,
    )
