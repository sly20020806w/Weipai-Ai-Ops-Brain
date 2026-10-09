"""Step 14：所有源系统用 Fake/HTTP mock 验收，阻止实际网络访问。"""

from typing import Literal

import httpx2 as httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.connectors.changes.argocd import HTTPArgoCDConnector
from app.connectors.changes.base import (
    ChangesError,
    ChangesHTTPError,
    ChangesNotFound,
    ChangesResponseError,
    ChangesTimeout,
)
from app.connectors.changes.ci import HTTPCIConnector
from app.connectors.changes.config import ArgoCDConfig, CIConfig, ConfigCenterConfig, GitConfig
from app.connectors.changes.config_center import HTTPConfigCenterConnector
from app.connectors.changes.factory import (
    create_argocd_connector,
    create_ci_connector,
    create_config_center_connector,
    create_git_connector,
)
from app.connectors.changes.fake import (
    SAMPLE_END,
    SAMPLE_PATCH,
    SAMPLE_START,
    FakeArgoCDConnector,
    FakeCIConnector,
    FakeConfigCenterConnector,
    FakeGitConnector,
)
from app.connectors.changes.git import HTTPGitConnector
from app.connectors.changes.models import ConfigVersion, DeploymentQuery, VersionQuery, config_diff
from app.connectors.models import ExecutorCredentials, ReaderCredentials
from app.tools.models import JsonObject

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
BINDINGS = {"payment-service": "weipai/payment-service"}
VERSION = VersionQuery(service_name="payment-service", from_version="v2.3.6", to_version="v2.3.7")
WINDOW = DeploymentQuery(service_name="payment-service", start=SAMPLE_START, end=SAMPLE_END)
GITLAB_DIFF: JsonObject = {
    "compare_timeout": False,
    "diffs": [{"old_path": "payment.yaml", "new_path": "payment.yaml", "diff": SAMPLE_PATCH}],
}
GITHUB_DIFF: JsonObject = {
    "files": [{"filename": "payment.yaml", "status": "modified", "patch": SAMPLE_PATCH}],
}


def credentials(name: str) -> ReaderCredentials:
    return ReaderCredentials(connector=name, token=SecretStr("mock-reader-secret"))


@pytest.mark.parametrize("provider", ["gitlab", "github"])
@pytest.mark.asyncio
async def test_git_native_path_auth_and_compare(provider: Literal["gitlab", "github"]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.host == "git.invalid"
        if provider == "gitlab":
            assert request.url.raw_path.startswith(
                b"/api/projects/weipai%2Fpayment-service/repository/compare?"
            )
            assert request.url.params["from"] == "v2.3.6" and request.url.params["to"] == "v2.3.7"
            assert request.url.params["straight"] == "true"
            assert request.headers["PRIVATE-TOKEN"] == "mock-reader-secret"
        else:
            assert request.url.path == "/api/repos/weipai/payment-service/compare/v2.3.6...v2.3.7"
            assert request.headers["Authorization"] == "Bearer mock-reader-secret"
            assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        return httpx.Response(200, json=GITLAB_DIFF if provider == "gitlab" else GITHUB_DIFF)

    async with HTTPGitConnector(
        GitConfig(base_url="https://git.invalid/api", services=BINDINGS, provider=provider),
        credentials("git"),
        transport=httpx.MockTransport(handler),
    ) as connector:
        result = await connector.compare_versions(VERSION)
        assert result.source == provider and result.files[0].patch == SAMPLE_PATCH
        assert result.from_version == "v2.3.6" and result.to_version == "v2.3.7"
        assert result.comparison_kind == ("direct" if provider == "gitlab" else "merge_base")
        assert "mock-reader-secret" not in result.model_dump_json()
    with pytest.raises(ChangesError, match="已关闭"):
        await connector.compare_versions(VERSION)


@pytest.mark.parametrize(
    "data",
    [
        {"compare_timeout": True, "diffs": []},
        {},
        {"compare_timeout": False, "diffs": "bad"},
        {"compare_timeout": False, "diffs": [{"collapsed": True}]},
        {"compare_timeout": False, "diffs": [{"too_large": True}]},
        {"compare_timeout": False, "diffs": [{"diff": None}]},
        {
            "compare_timeout": False,
            "diffs": [
                {"old_path": "a", "new_path": "a", "diff": ""},
                {"old_path": "a", "new_path": "a", "diff": ""},
            ],
        },
    ],
)
@pytest.mark.asyncio
async def test_gitlab_rejects_incomplete_diff(data: JsonObject) -> None:
    async with HTTPGitConnector(
        GitConfig(base_url="https://git.invalid", services=BINDINGS, provider="gitlab"),
        credentials("git"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)),
    ) as connector:
        with pytest.raises(ChangesResponseError):
            await connector.compare_versions(VERSION)


@pytest.mark.asyncio
async def test_github_binary_and_truncation() -> None:
    binary: JsonObject = {"files": [{"filename": "image.png", "status": "added"}]}
    async with HTTPGitConnector(
        GitConfig(base_url="https://git.invalid", services=BINDINGS, provider="github"),
        credentials("git"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=binary)),
    ) as connector:
        result = await connector.compare_versions(VERSION)
        assert result.files[0].patch == "" and not result.files[0].content_available
        binary["files"] = [{"filename": str(i), "status": "added"} for i in range(300)]
        with pytest.raises(ChangesResponseError, match="截断上限"):
            await connector.compare_versions(VERSION)


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
@pytest.mark.asyncio
async def test_http_errors_do_not_leak_or_redirect(status: int) -> None:
    async with HTTPGitConnector(
        GitConfig(base_url="https://git.invalid", services=BINDINGS, provider="gitlab"),
        credentials("git"),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                status,
                text="mock-reader-secret",
                headers={"Location": "https://other.invalid"},
            )
        ),
    ) as connector:
        with pytest.raises(ChangesNotFound if status == 404 else ChangesHTTPError) as error:
            await connector.compare_versions(VERSION)
        assert "mock-reader-secret" not in str(error.value)


@pytest.mark.parametrize("failure", ["timeout", "transport", "json"])
@pytest.mark.asyncio
async def test_transport_failures_are_sanitized(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("mock-reader-secret", request=request)
        if failure == "transport":
            raise httpx.ConnectError("mock-reader-secret", request=request)
        return httpx.Response(200, text="mock-reader-secret")

    async with HTTPGitConnector(
        GitConfig(base_url="https://git.invalid", services=BINDINGS, provider="gitlab"),
        credentials("git"),
        transport=httpx.MockTransport(handler),
    ) as connector:
        with pytest.raises(ChangesTimeout if failure == "timeout" else ChangesError) as error:
            await connector.compare_versions(VERSION)
        assert "mock-reader-secret" not in str(error.value)


@pytest.mark.parametrize("provider", ["jenkins", "gitlab_ci"])
@pytest.mark.asyncio
async def test_ci_native_pagination_window_and_order(
    provider: Literal["jenkins", "gitlab_ci"],
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        if provider == "gitlab_ci":
            page = int(request.url.params["page"])
            assert request.headers["PRIVATE-TOKEN"] == "mock-reader-secret"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": page,
                        "sha": f"sha{page}",
                        "status": "success",
                        "created_at": f"2026-10-01T0{page}:00:00Z",
                    }
                ],
                headers={"X-Next-Page": "2" if page == 1 else ""},
            )
        assert request.url.path == "/job/weipai/job/payment-service/api/json"
        assert request.headers["Authorization"].startswith("Basic ")
        assert f"{{{len(calls) - 1},{len(calls)}}}" in request.url.params["tree"]
        return httpx.Response(
            200,
            json={
                "builds": (
                    [
                        {
                            "number": 1,
                            "timestamp": int(SAMPLE_START.timestamp() * 1000),
                            "result": "SUCCESS",
                            "building": False,
                            "actions": [{"lastBuiltRevision": {"SHA1": "sha1"}}],
                        }
                    ]
                    if len(calls) == 1
                    else []
                )
            },
        )

    async with HTTPCIConnector(
        CIConfig(
            base_url="https://ci.invalid",
            services=BINDINGS,
            provider=provider,
            username="reader" if provider == "jenkins" else None,
            page_size=1,
        ),
        credentials("ci"),
        transport=httpx.MockTransport(handler),
    ) as connector:
        records = await connector.list_builds(WINDOW)
        assert len(calls) == 2 and len(records) == 1 and records[0].revision == "sha1"
        assert records[0].timestamp.tzinfo is SAMPLE_START.tzinfo


@pytest.mark.parametrize("next_page", [None, "1", "https://other.invalid", "2"])
@pytest.mark.asyncio
async def test_ci_pagination_incomplete_rejected(next_page: str | None) -> None:
    headers = {} if next_page is None else {"X-Next-Page": next_page}
    async with HTTPCIConnector(
        CIConfig(
            base_url="https://ci.invalid", services=BINDINGS, provider="gitlab_ci", max_pages=1
        ),
        credentials("ci"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[], headers=headers)),
    ) as connector:
        with pytest.raises(ChangesResponseError):
            await connector.list_builds(WINDOW)


@pytest.mark.asyncio
async def test_argocd_identity_retained_history_and_reverse_order() -> None:
    data: JsonObject = {
        "metadata": {"name": "payment"},
        "status": {
            "history": [
                {"id": 1, "revision": "sha1", "deployedAt": "2026-10-01T08:00:00+08:00"},
                {"id": 2, "revision": "sha2", "deployedAt": "2026-10-01T01:00:00Z"},
                {"id": 3, "revision": "sha3", "deployedAt": "2026-10-01T02:00:00Z"},
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/applications/payment"
        assert request.method == "GET" and not request.url.query
        return httpx.Response(200, json=data)

    async with HTTPArgoCDConnector(
        ArgoCDConfig(base_url="https://argo.invalid", services={"payment-service": "payment"}),
        credentials("argocd"),
        transport=httpx.MockTransport(handler),
    ) as connector:
        result = await connector.list_deployments(WINDOW)
        assert [item.id for item in result] == ["2", "1"]
        data["metadata"] = {"name": "wrong"}
        with pytest.raises(ChangesResponseError, match="绑定不符"):
            await connector.list_deployments(WINDOW)


@pytest.mark.asyncio
async def test_config_center_allowlist_and_version_identity() -> None:
    mismatch = False

    def handler(request: httpx.Request) -> httpx.Response:
        version = request.url.path.split("/")[-1]
        assert request.method == "GET" and request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json={
                "service_name": "wrong" if mismatch else "payment-service",
                "version": version,
                "values": {
                    "db.pool.max_connections": "50" if version == "v2.3.6" else "500",
                    "db.password": "source-secret",
                },
            },
        )

    async with HTTPConfigCenterConnector(
        ConfigCenterConfig(
            base_url="https://config.invalid",
            services=BINDINGS,
            allowed_keys=("db.pool.max_connections",),
        ),
        credentials("config_center"),
        transport=httpx.MockTransport(handler),
    ) as connector:
        result = await connector.compare_versions(VERSION)
        assert result.changes[0].before == "50" and result.changes[0].after == "500"
        assert "source-secret" not in result.model_dump_json()
        mismatch = True
        with pytest.raises(ChangesResponseError):
            await connector.compare_versions(VERSION)


@pytest.mark.parametrize(
    "url",
    [
        "http://git.invalid",
        "https://u:p@git.invalid",
        "https://git.invalid?token=x",
        "https://git.invalid/#x",
        "https://git.invalid/../api",
        "https://git.invalid/%2e/api",
        "https://git.invalid\\evil",
        "https://git.invalid:0",
    ],
)
def test_unsafe_endpoints_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        GitConfig(base_url=url, services=BINDINGS, provider="gitlab")


@pytest.mark.parametrize("target", ["../other", "a//b", "https://other.invalid", "x?y", "x%2fy"])
def test_unsafe_bindings_rejected(target: str) -> None:
    with pytest.raises(ValidationError):
        GitConfig(
            base_url="https://git.invalid", services={"payment-service": target}, provider="gitlab"
        )


@pytest.mark.parametrize(
    "key", ["db.password", "api_token", "secret", "access_key_id", "apiKey", "privateKey"]
)
def test_credentials_cannot_be_config_allowlist(key: str) -> None:
    with pytest.raises(ValidationError):
        ConfigCenterConfig(
            base_url="https://config.invalid", services=BINDINGS, allowed_keys=(key,)
        )
    with pytest.raises(ValidationError):
        ConfigVersion(service_name="payment-service", version="v1", values={key: "secret-value"})


@pytest.mark.parametrize("reference", ["../main", "x..y", "x?token=x", "x//y", "https://x"])
def test_invalid_ref_rejected(reference: str) -> None:
    with pytest.raises(ValidationError):
        VersionQuery(service_name="payment-service", from_version=reference, to_version="v2.3.7")


@pytest.mark.parametrize("provider", ["gitlab", "github"])
@pytest.mark.asyncio
async def test_fake_compare_and_snapshot_isolation(provider: Literal["gitlab", "github"]) -> None:
    async with FakeGitConnector(provider=provider) as git, FakeConfigCenterConnector() as config:
        code = await git.compare_versions(VERSION)
        assert (
            code.source == provider and "50" in code.files[0].patch and "500" in code.files[0].patch
        )
        result = await config.compare_versions(VERSION)
        assert [(c.key, c.before, c.after) for c in result.changes] == [
            ("db.pool.max_connections", "50", "500")
        ]
        with pytest.raises(ChangesNotFound):
            await git.compare_versions(VERSION.model_copy(update={"service_name": "other"}))
        with pytest.raises(ChangesNotFound):
            await config.compare_versions(VERSION.model_copy(update={"to_version": "v9"}))
    with pytest.raises(ChangesError):
        await config.compare_versions(VERSION)


@pytest.mark.asyncio
async def test_fake_sort_window_and_empty_service() -> None:
    async with FakeArgoCDConnector() as argo, FakeCIConnector() as ci:
        assert [r.id for r in await argo.list_deployments(WINDOW)] == ["37", "36"]
        assert [r.id for r in await ci.list_builds(WINDOW)] == ["37", "36"]
        other = WINDOW.model_copy(update={"service_name": "other"})
        assert await argo.list_deployments(other) == ()
        assert await ci.list_builds(other) == ()


def test_config_added_removed_unchanged() -> None:
    before = ConfigVersion(
        service_name="payment-service", version="v1", values={"keep": "1", "remove": "2"}
    )
    after = ConfigVersion(
        service_name="payment-service", version="v2", values={"keep": "1", "add": "3"}
    )
    assert [(c.key, c.before, c.after) for c in config_diff(before, after).changes] == [
        ("add", None, "3"),
        ("remove", "2", None),
    ]


@pytest.mark.asyncio
async def test_factories_default_fake_real_interfaces_and_reader_separation() -> None:
    factories = (
        create_git_connector,
        create_ci_connector,
        create_argocd_connector,
        create_config_center_connector,
    )
    fake_types = (FakeGitConnector, FakeCIConnector, FakeArgoCDConnector, FakeConfigCenterConnector)
    config = Settings(APP_ENV="test")
    for factory, kind in zip(factories, fake_types, strict=True):
        async with factory(config) as connector:
            assert isinstance(connector, kind) and connector.reader_credentials is None
            assert not any(
                name in dir(connector)
                for name in ("sync", "deploy", "trigger_build", "write", "delete")
            )
    for git_provider in ("gitlab", "github"):
        for ci_provider in ("gitlab_ci", "jenkins"):
            settings = Settings(
                APP_ENV="staging",
                CONNECTOR_MODE="real",
                CONNECTOR_READER_TOKENS={
                    name: "mock-reader-secret" for name in ("git", "ci", "argocd", "config_center")
                },
                GIT_CONFIG={
                    "base_url": "https://git.invalid",
                    "services": BINDINGS,
                    "provider": git_provider,
                },
                CI_CONFIG={
                    "base_url": "https://ci.invalid",
                    "services": BINDINGS,
                    "provider": ci_provider,
                    "username": "reader",
                },
                ARGOCD_CONFIG={"base_url": "https://argo.invalid", "services": BINDINGS},
                CONFIG_CENTER_CONFIG={
                    "base_url": "https://config.invalid",
                    "services": BINDINGS,
                    "allowed_keys": ["db.pool.max_connections"],
                },
            )
            for factory in factories:
                async with factory(
                    settings, transport=httpx.MockTransport(lambda r: httpx.Response(500))
                ) as connector:
                    assert connector.reader_credentials is not None
    with pytest.raises(ValidationError, match="只允许 fake"):
        Settings(APP_ENV="local", CONNECTOR_MODE="real")
    with pytest.raises(ValueError, match="git 的 Reader"):
        HTTPGitConnector(
            GitConfig(base_url="https://git.invalid", services=BINDINGS, provider="gitlab"),
            credentials("ci"),
        )
    with pytest.raises(TypeError, match="ReaderCredentials"):
        HTTPGitConnector(
            GitConfig(base_url="https://git.invalid", services=BINDINGS, provider="gitlab"),
            ExecutorCredentials(connector="git", token=SecretStr("executor-secret")),  # type: ignore[arg-type]
        )
