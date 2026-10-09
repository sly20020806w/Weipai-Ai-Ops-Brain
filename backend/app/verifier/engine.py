"""独立验证引擎只经 Dispatcher 获取八项证据，不采信主 Agent 的恢复判断。"""

import json
from collections.abc import Callable

from app.connectors.cloud.models import CloudResources
from app.tools.cloud import CloudResourcesOutput
from app.tools.dispatcher import ToolDispatcher
from app.tools.kubernetes import KubernetesStatus, ServiceRuntime
from app.tools.models import DispatchStatus, JsonObject, ToolModel
from app.tools.observability import LogsOutput, MetricsOutput, TracesOutput
from app.tools.registry import json_object
from app.verifier.checks import (
    deployment_healthy,
    logs_healthy,
    metrics_healthy,
    pods_healthy,
    resources_healthy,
    traces_healthy,
)
from app.verifier.models import (
    VerificationCheck,
    VerificationConfig,
    VerificationReport,
    VerificationSpec,
    criteria_hash,
)

FACT_TOOLS = frozenset(
    {
        "get_k8s_status",
        "get_service_runtime",
        "query_metrics",
        "query_logs",
        "query_traces",
        "get_cloud_resources",
    }
)


class VerificationEngine:
    def __init__(self, dispatcher: ToolDispatcher, config: VerificationConfig) -> None:
        self.dispatcher = dispatcher
        self.config = VerificationConfig.model_validate(config)

    async def evaluate(self, spec: VerificationSpec) -> VerificationReport:
        spec = VerificationSpec.model_validate(spec)
        checks: list[VerificationCheck] = []

        async def check[Output: ToolModel](
            name: str,
            tool: str,
            parameters: JsonObject,
            output: type[Output],
            predicate: Callable[[Output], bool],
        ) -> None:
            result = await self.dispatcher.dispatch(
                task_id=spec.task_id,
                tool_name=tool,
                parameters=parameters,
                actor="verifier",
                allowed_tools=FACT_TOOLS,
            )
            passed = False
            if result.status is DispatchStatus.SUCCEEDED and result.result is not None:
                passed = predicate(output.model_validate_json(json.dumps(result.result)))
            checks.append(
                VerificationCheck(
                    name=name,
                    passed=passed,
                    evidence_id=result.evidence_id,
                    reason="恢复标准通过" if passed else "未恢复、数据不足或查询被拒绝",
                )
            )

        k8s: JsonObject = {"namespace": spec.namespace, "service_name": spec.service_name}
        window: JsonObject = json_object(
            {
                "service_name": spec.service_name,
                "start": spec.start.isoformat(),
                "end": spec.end.isoformat(),
            }
        )
        await check(
            "deployment",
            "get_k8s_status",
            k8s,
            KubernetesStatus,
            lambda data: deployment_healthy(spec, data),
        )
        await check(
            "pods",
            "get_service_runtime",
            k8s,
            ServiceRuntime,
            lambda data: pods_healthy(spec, data),
        )
        for metric in ("http_5xx_ratio", "http_p99_ms", "http_success_ratio"):

            def healthy(data: MetricsOutput, selected: str = metric) -> bool:
                return metrics_healthy(spec, self.config, data, selected)

            await check(
                metric,
                "query_metrics",
                {
                    **window,
                    "metric_name": metric,
                    "step_seconds": self.config.step_seconds,
                },
                MetricsOutput,
                healthy,
            )
        await check("logs", "query_logs", window, LogsOutput, lambda data: logs_healthy(spec, data))
        await check(
            "traces",
            "query_traces",
            window,
            TracesOutput,
            lambda data: traces_healthy(spec, self.config, data),
        )
        await check(
            "resources",
            "get_cloud_resources",
            window,
            CloudResourcesOutput,
            lambda data: resources_healthy(
                spec, self.config, CloudResources.model_validate(data.model_dump())
            ),
        )
        return VerificationReport(
            spec=spec, criteria_hash=criteria_hash(self.config), checks=tuple(checks)
        )
