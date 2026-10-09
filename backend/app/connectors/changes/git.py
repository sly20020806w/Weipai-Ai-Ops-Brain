"""GitLab 直接比较和 GitHub merge-base 比较；明确拒绝被截断的文件清单。"""

from typing import Literal
from urllib.parse import quote

import httpx2 as httpx
from pydantic import TypeAdapter

from app.connectors.changes.base import ChangesResponseError, GitConnector
from app.connectors.changes.config import GitConfig
from app.connectors.changes.http import ReaderHTTP, instant, obj, payload, rows, string, unique
from app.connectors.changes.models import (
    ChangeRecord,
    CodeComparison,
    DeploymentQuery,
    FileDiff,
    Repository,
    ServiceName,
    VersionQuery,
)
from app.connectors.models import ReaderCredentials


class HTTPGitConnector(GitConnector):
    def __init__(
        self,
        config: GitConfig,
        credentials: ReaderCredentials,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        super().__init__(credentials)
        self._config = GitConfig.model_validate(config)
        self._http = ReaderHTTP(self._config, credentials, "git", transport)

    async def aclose(self) -> None:
        await self._http.client.aclose()

    async def list_changes(self, query: DeploymentQuery) -> tuple[ChangeRecord, ...]:
        query = DeploymentQuery.model_validate(query)
        target = self._http.target(query.service_name)
        provider = self._config.provider
        if provider == "gitlab":
            path = f"projects/{quote(target, safe='')}/repository/commits"
        else:
            if len(target.split("/")) != 2:
                raise ChangesResponseError("GitHub 仓库绑定无效")
            path = (
                "repos/" + "/".join(quote(part, safe="") for part in target.split("/")) + "/commits"
            )
        records: list[ChangeRecord] = []
        seen: list[str] = []
        for page in range(1, self._config.max_pages + 1):
            params = {
                "since": query.start.isoformat(),
                "until": query.end.isoformat(),
                "per_page": str(self._config.page_size),
                "page": str(page),
            }
            if provider == "gitlab":
                params["all"] = "true"
            items = rows(payload(await self._http.read(path, params)))
            for item in items:
                identity = string(item.get("id") if provider == "gitlab" else item.get("sha"))
                seen.append(identity)
                if provider == "gitlab":
                    timestamp = instant(item.get("committed_date"))
                    parents = item.get("parent_ids")
                    if not isinstance(parents, list) or any(
                        not isinstance(p, str) for p in parents
                    ):
                        raise ChangesResponseError("Git 父提交列表无效")
                    parent_count = len(parents)
                else:
                    timestamp = instant(obj(obj(item.get("commit")).get("committer")).get("date"))
                    github_parents = rows(item.get("parents"))
                    for parent in github_parents:
                        string(parent.get("sha"))
                    parent_count = len(github_parents)
                if query.contains(timestamp):
                    records.append(
                        ChangeRecord(
                            id=identity,
                            service_name=query.service_name,
                            source=provider,
                            kind="Merge" if parent_count > 1 else "Commit",
                            timestamp=timestamp,
                            source_ref=f"{provider}:{target}:commit:{identity}",
                            revision=identity,
                        )
                    )
            unique(seen)
            # 固定本地页码，不追随源响应的分页 URL；满页继续读到短页。
            if len(items) < self._config.page_size:
                return tuple(records)
        raise ChangesResponseError("Git 历史超出分页上限，结果不完整")

    async def get_repository(self, service_name: str) -> Repository:
        TypeAdapter(ServiceName).validate_python(service_name, strict=True)
        target = self._http.target(service_name)
        if self._config.provider == "gitlab":
            path = f"projects/{quote(target, safe='')}"
            data = obj(payload(await self._http.read(path)))
            record_id = data.get("id")
            if type(record_id) is not int or record_id <= 0:
                raise ChangesResponseError("GitLab 项目 ID 无效")
            name = string(data.get("path_with_namespace"))
            if target not in {str(record_id), name}:
                raise ChangesResponseError("GitLab 返回未绑定项目")
        else:
            parts = target.split("/")
            if len(parts) != 2:
                raise ChangesResponseError("GitHub 仓库绑定无效")
            path = "repos/" + "/".join(quote(part, safe="") for part in parts)
            data = obj(payload(await self._http.read(path)))
            record_id = data.get("id")
            if type(record_id) is not int or record_id <= 0:
                raise ChangesResponseError("GitHub 仓库 ID 无效")
            name = string(data.get("full_name"))
            if name.lower() != target.lower():
                raise ChangesResponseError("GitHub 返回未绑定仓库")
        return Repository(
            service_name=service_name,
            source=self._config.provider,
            repository_id=str(record_id),
            name=name,
            source_ref=f"{self._config.provider}:{record_id}",
        )

    async def compare_versions(self, query: VersionQuery) -> CodeComparison:
        query = VersionQuery.model_validate(query)
        target = self._http.target(query.service_name)
        files: list[FileDiff] = []
        kind: Literal["direct", "merge_base"]
        if self._config.provider == "gitlab":
            path = f"projects/{quote(target, safe='')}/repository/compare"
            data = obj(
                payload(
                    await self._http.read(
                        path,
                        {
                            "from": query.from_version,
                            "to": query.to_version,
                            "straight": "true",
                        },
                    )
                )
            )
            if data.get("compare_timeout") is not False:
                raise ChangesResponseError("GitLab 比较不完整或缺少完整性标记")
            for item in rows(data.get("diffs")):
                if (
                    item.get("collapsed", False) is not False
                    or item.get("too_large", False) is not False
                ):
                    raise ChangesResponseError("GitLab 文件 diff 被截断")
                patch = item.get("diff")
                if not isinstance(patch, str):
                    raise ChangesResponseError("GitLab diff 字段缺失")
                files.append(
                    FileDiff(
                        old_path=string(item.get("old_path")),
                        new_path=string(item.get("new_path")),
                        patch=patch,
                        status=(
                            "added"
                            if item.get("new_file") is True
                            else "removed"
                            if item.get("deleted_file") is True
                            else "renamed"
                            if item.get("renamed_file") is True
                            else "modified"
                        ),
                    )
                )
            kind = "direct"
        else:
            parts = target.split("/")
            if len(parts) != 2:
                raise ValueError("GitHub 服务绑定必须是 owner/repository")
            repository = "/".join(quote(part, safe="") for part in parts)
            path = (
                f"repos/{repository}/compare/{quote(query.from_version, safe='')}"
                f"...{quote(query.to_version, safe='')}"
            )
            # GitHub 只在第一页给 files（最多 300）；无需拉取 commits 的分页。
            data = obj(payload(await self._http.read(path, {"per_page": "1", "page": "1"})))
            source_files = rows(data.get("files"))
            if len(source_files) >= 300:
                raise ChangesResponseError("GitHub 文件列表达到截断上限，不能声称完整")
            statuses: TypeAdapter[Literal["added", "removed", "renamed", "modified"]] = TypeAdapter(
                Literal["added", "removed", "renamed", "modified"]
            )
            for item in source_files:
                status = string(item.get("status"))
                if status not in {"added", "removed", "renamed", "modified"}:
                    raise ChangesResponseError("GitHub 文件状态不受支持")
                name = string(item.get("filename"))
                patch = item.get("patch")
                if patch is not None and not isinstance(patch, str):
                    raise ChangesResponseError("GitHub patch 字段无效")
                files.append(
                    FileDiff(
                        old_path=string(item.get("previous_filename"))
                        if status == "renamed"
                        else name,
                        new_path=name,
                        patch=patch or "",
                        status=statuses.validate_python(status),
                        content_available=patch is not None,
                    )
                )
            kind = "merge_base"
        unique([item.new_path for item in files])
        return CodeComparison(
            **query.model_dump(),
            source=self._config.provider,
            repository=target,
            files=tuple(files),
            comparison_kind=kind,
            source_ref=f"{self._config.provider}:{target}:{query.from_version}..{query.to_version}",
        )
