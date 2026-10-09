"""L0 verify_action 只返回独立验证报告；状态迁移仍由 Verifier 经 tasks 服务完成。"""

from app.policy.models import RiskLevel
from app.tools.registry import ToolRegistry
from app.verifier.engine import VerificationEngine
from app.verifier.models import VerificationReport, VerificationSpec


def register_verification_tool(registry: ToolRegistry, engine: VerificationEngine) -> None:
    registry.register(
        name="verify_action",
        description="独立验证目标版本、资源与动作后业务指标的恢复证据",
        input_model=VerificationSpec,
        output_model=VerificationReport,
        handler=engine.evaluate,
        risk_level=RiskLevel.L0,
    )
