"""按需读取的代码、构建、发布和配置差异快照。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ServiceName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Reference = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,255}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=512)]


def non_secret_config_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    for key in keys:
        normalized = key.lower().replace("_", "").replace(".", "").replace("-", "")
        if not key or any(
            part in normalized
            for part in (
                "password",
                "passwd",
                "secret",
                "token",
                "credential",
                "privatekey",
                "accesskey",
                "apikey",
            )
        ):
            raise ValueError("配置快照和白名单不能包含凭证或密钥字段")
    return keys


class Snapshot(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class VersionQuery(Snapshot):
    service_name: ServiceName
    from_version: Reference
    to_version: Reference

    @field_validator("from_version", "to_version")
    @classmethod
    def safe_ref(cls, value: str) -> str:
        if any(part in {"", ".", ".."} for part in value.split("/")) or ".." in value:
            raise ValueError("版本引用不能包含空段或路径跳转")
        return value


class DeploymentQuery(Snapshot):
    service_name: ServiceName
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self) -> "DeploymentQuery":
        if not timedelta(0) < self.end - self.start <= timedelta(days=30):
            raise ValueError("时间窗必须 start < end 且不超过 30 天")
        return self

    def contains(self, value: datetime) -> bool:
        return self.start <= value < self.end


class FileDiff(Snapshot):
    old_path: Identifier
    new_path: Identifier
    patch: str = Field(max_length=1_000_000)
    status: Literal["added", "modified", "removed", "renamed"]
    content_available: bool = True


class CodeComparison(VersionQuery):
    source: Literal["gitlab", "github"]
    repository: Identifier
    files: tuple[FileDiff, ...]
    # GitHub 是 merge-base 比较；明确保留语义，避免误当成直接配置差异。
    comparison_kind: Literal["direct", "merge_base"]
    source_ref: Identifier


class Repository(Snapshot):
    service_name: ServiceName
    source: Literal["gitlab", "github"]
    repository_id: Identifier
    name: Identifier
    source_ref: Identifier


class ConfigChange(Snapshot):
    key: Identifier
    before: str | None
    after: str | None


class ConfigComparison(VersionQuery):
    source: Literal["config_center"] = "config_center"
    changes: tuple[ConfigChange, ...]
    source_ref: Identifier


class TimedRecord(Snapshot):
    id: Identifier
    service_name: ServiceName
    timestamp: AwareDatetime
    source_ref: Identifier

    @field_validator("timestamp")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class BuildRecord(TimedRecord):
    source: Literal["jenkins", "gitlab_ci"]
    revision: Identifier | None
    status: Identifier


class DeploymentRecord(TimedRecord):
    source: Literal["argocd"] = "argocd"
    application: Identifier
    revision: Identifier


class ChangeRecord(TimedRecord):
    """历史变更只携带引用，不携带日志、配置值、凭证或提交正文。"""

    source: Literal["gitlab", "github", "config_center"]
    kind: Literal["Commit", "Merge", "Config"]
    revision: Identifier | None = None

    @model_validator(mode="after")
    def source_kind(self) -> "ChangeRecord":
        if (self.source == "config_center") != (self.kind == "Config"):
            raise ValueError("变更类型与源系统不符")
        return self


class ConfigVersion(Snapshot):
    service_name: ServiceName
    version: Reference
    # 只允许非敏感白名单配置；凭证和密钥不能进入快照。
    values: dict[str, str]

    @field_validator("values")
    @classmethod
    def non_secret_values(cls, value: dict[str, str]) -> dict[str, str]:
        non_secret_config_keys(tuple(value))
        return value


def config_diff(before: ConfigVersion, after: ConfigVersion) -> ConfigComparison:
    if before.service_name != after.service_name:
        raise ValueError("配置版本必须属于同一个服务")
    changes = tuple(
        ConfigChange(key=key, before=before.values.get(key), after=after.values.get(key))
        for key in sorted(before.values.keys() | after.values.keys())
        if before.values.get(key) != after.values.get(key)
    )
    return ConfigComparison(
        service_name=before.service_name,
        from_version=before.version,
        to_version=after.version,
        changes=changes,
        source_ref=f"config_center:{before.service_name}:{before.version}..{after.version}",
    )
