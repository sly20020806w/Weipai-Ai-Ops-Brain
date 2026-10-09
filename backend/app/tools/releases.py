"""发布只读高级查询，业务层不直接调用源 API。"""

from app.connectors.changes.releases import ReleaseReader
from app.policy.models import RiskLevel
from app.tasks.releases.models import (
    ReleaseManifest,
    ReleaseObservation,
    ReleaseQuery,
    ReleaseWindow,
)
from app.tools.registry import ToolRegistry


def register_release_reads(registry: ToolRegistry, reader: ReleaseReader) -> None:
    registry.register(
        name="get_release_request",
        description="读取源平台发布申请与 SQL/资源/监控/回滚材料",
        input_model=ReleaseQuery,
        output_model=ReleaseManifest,
        handler=reader.manifest,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="query_release_observation",
        description="读取发布 UTC 窗口内运行状态、业务指标、日志、Trace 和资源摘要",
        input_model=ReleaseWindow,
        output_model=ReleaseObservation,
        handler=reader.observe,
        risk_level=RiskLevel.L0,
    )
