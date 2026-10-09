"""离线样例；同一验收脚本用专项测试验证 Dispatcher 证据和审计。"""

import asyncio
import json
from contextlib import AsyncExitStack

from app.config import Settings
from app.connectors.changes.factory import (
    create_argocd_connector,
    create_ci_connector,
    create_config_center_connector,
    create_git_connector,
)
from app.connectors.changes.fake import SAMPLE_END, SAMPLE_START
from app.connectors.changes.models import DeploymentQuery, VersionQuery
from app.tools.changes import register_change_tools
from app.tools.registry import ToolRegistry


async def main() -> None:
    settings = Settings(
        APP_ENV="local",
        CONNECTOR_MODE="fake",
        CONNECTOR_READER_TOKENS={},
        DATABASE_URL=None,
        GIT_CONFIG=None,
        CI_CONFIG=None,
        ARGOCD_CONFIG=None,
        CONFIG_CENTER_CONFIG=None,
        OPS_PLATFORM_CONFIG=None,
        KUBERNETES_CONFIG=None,
        PROMETHEUS_CONFIG=None,
        SLS_CONFIG=None,
        ARMS_CONFIG=None,
        LLM_MODE="fake",
    )
    async with AsyncExitStack() as stack:
        git = await stack.enter_async_context(create_git_connector(settings))
        ci = await stack.enter_async_context(create_ci_connector(settings))
        argo = await stack.enter_async_context(create_argocd_connector(settings))
        config = await stack.enter_async_context(create_config_center_connector(settings))
        registry = ToolRegistry()
        register_change_tools(registry, git, ci, argo, config)
        versions = VersionQuery(
            service_name="payment-service", from_version="v2.3.6", to_version="v2.3.7"
        )
        window = DeploymentQuery(service_name="payment-service", start=SAMPLE_START, end=SAMPLE_END)
        code = await git.compare_versions(versions)
        configuration = await config.compare_versions(versions)
        deployments, builds = await argo.list_deployments(window), await ci.list_builds(window)
        assert configuration.changes[0].before == "50" and configuration.changes[0].after == "500"
        assert [item.id for item in deployments] == ["37", "36"]
        print(
            json.dumps(
                {
                    "mode": "fake",
                    "tools": [item.name for item in registry.declarations()],
                    "code": code.model_dump(mode="json"),
                    "configuration": configuration.model_dump(mode="json"),
                    "deployments": [item.model_dump(mode="json") for item in deployments],
                    "builds": [item.model_dump(mode="json") for item in builds],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    print("Step 14 Fake 样例验收通过（未连接真实系统）")


if __name__ == "__main__":
    asyncio.run(main())
