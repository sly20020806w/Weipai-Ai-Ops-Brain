"""两个 L0 高级查询；统一 Dispatcher 负责 Policy、Evidence、审计与 Replay。"""

from typing import Literal

from pydantic import model_validator

from app.connectors.changes.base import (
    ArgoCDConnector,
    CIConnector,
    ConfigCenterConnector,
    GitConnector,
)
from app.connectors.changes.models import (
    BuildRecord,
    CodeComparison,
    ConfigComparison,
    DeploymentQuery,
    DeploymentRecord,
    VersionQuery,
)
from app.policy.models import RiskLevel
from app.tools.models import ToolModel
from app.tools.registry import ToolRegistry


class CompareVersionsInput(VersionQuery, ToolModel):
    pass


class CompareVersionsOutput(VersionQuery, ToolModel):
    code: CodeComparison
    configuration: ConfigComparison

    @model_validator(mode="after")
    def matching_versions(self) -> "CompareVersionsOutput":
        for result in (self.code, self.configuration):
            if (result.service_name, result.from_version, result.to_version) != (
                self.service_name,
                self.from_version,
                self.to_version,
            ):
                raise ValueError("比较结果与服务或版本不符")
        return self


class RecentDeploymentsInput(DeploymentQuery, ToolModel):
    pass


class RecentDeploymentsOutput(DeploymentQuery, ToolModel):
    history_scope: Literal["source_retained_history"] = "source_retained_history"
    deployments: tuple[DeploymentRecord, ...]
    builds: tuple[BuildRecord, ...]

    @model_validator(mode="after")
    def scoped_records(self) -> "RecentDeploymentsOutput":
        for records in (self.deployments, self.builds):
            if any(
                record.service_name != self.service_name or not self.contains(record.timestamp)
                for record in records
            ):
                raise ValueError("发布或构建结果超出服务与时间范围")
            if len({record.id for record in records}) != len(records):
                raise ValueError("发布或构建结果包含重复记录")
            if list(records) != sorted(
                records, key=lambda record: (record.timestamp, record.id), reverse=True
            ):
                raise ValueError("发布或构建结果必须按时间倒序")
        return self


def register_change_tools(
    registry: ToolRegistry,
    git: GitConnector,
    ci: CIConnector,
    argocd: ArgoCDConnector,
    config_center: ConfigCenterConnector,
) -> None:
    async def compare(query: CompareVersionsInput) -> CompareVersionsOutput:
        request = VersionQuery.model_validate(query.model_dump())
        return CompareVersionsOutput(
            **query.model_dump(),
            code=await git.compare_versions(request),
            configuration=await config_center.compare_versions(request),
        )

    async def recent(query: RecentDeploymentsInput) -> RecentDeploymentsOutput:
        request = DeploymentQuery.model_validate(query.model_dump())
        return RecentDeploymentsOutput(
            **query.model_dump(),
            deployments=await argocd.list_deployments(request),
            builds=await ci.list_builds(request),
        )

    registry.register(
        name="get_recent_deployments",
        description="按服务与 UTC 时间窗倒序读取发布和构建记录",
        input_model=RecentDeploymentsInput,
        output_model=RecentDeploymentsOutput,
        handler=recent,
        risk_level=RiskLevel.L0,
    )
    registry.register(
        name="compare_versions",
        description="读取指定服务两个版本的代码与非敏感配置差异",
        input_model=CompareVersionsInput,
        output_model=CompareVersionsOutput,
        handler=compare,
        risk_level=RiskLevel.L0,
    )
