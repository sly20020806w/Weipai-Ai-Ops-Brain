"""Step 19 离线契约：Fake、HTTP mock、UTC、边界与确定性 Workflow。"""

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Literal
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from pydantic import SecretStr, ValidationError
from temporalio.exceptions import ApplicationError

from app.config import Settings, parse_database_url
from app.connectors.changes.base import ChangesResponseError
from app.connectors.changes.config import ConfigCenterConfig, GitConfig
from app.connectors.changes.config_center import HTTPConfigCenterConnector
from app.connectors.changes.git import HTTPGitConnector
from app.connectors.changes.models import DeploymentQuery
from app.connectors.kubernetes.fake import FakeKubernetesConnector, timeline_snapshot
from app.connectors.kubernetes.models import KubernetesSnapshot
from app.connectors.models import ReaderCredentials
from app.db.session import Database
from app.graph.changes import activities as activity_module
from app.graph.changes.activities import TimelineActivities
from app.graph.changes.schemas import ChangeFact, ChangeSnapshot, TimelineInput, TimelineRequest
from app.graph.changes.sources import configured_sources
from app.graph.changes.workflow import ChangeTimelineWorkflow
from app.tools.timeline import RecentChangesInput

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
START = datetime(2026, 10, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=1)
CORE = ["Commit", "Merge", "Build", "Image", "Sync", "Deploy"]


async def snapshot(service: str = "payment-service") -> ChangeSnapshot:
    async with AsyncExitStack() as stack:
        sources = await configured_sources(stack, Settings(APP_ENV="test"))
        return await sources.collect(DeploymentQuery(service_name=service, start=START, end=END))


@pytest.mark.asyncio
async def test_fake_all_six_sources_and_chain_order_and_stable_time() -> None:
    first = await snapshot()
    second = await snapshot()
    assert first == second and len(first.events) == 8
    assert [e.kind for e in first.events if e.kind in CORE] == CORE
    assert {e.source for e in first.events} == {
        "gitlab",
        "gitlab_ci",
        "argocd",
        "config_center",
        "kubernetes",
        "alibaba_cloud",
    }
    assert not first.missing_bindings
    assert all(e.occurred_at.tzinfo is UTC and e.source_ref for e in first.events)


@pytest.mark.asyncio
async def test_aggregated_kubernetes_events_preserve_first_time_and_skip_unstable_time() -> None:
    async with AsyncExitStack() as stack:
        sources = await configured_sources(stack, Settings(APP_ENV="test"))
        query = DeploymentQuery(service_name="payment-service", start=START, end=END)
        before = await sources.collect(query)
        original = timeline_snapshot()
        later = tuple(
            e.model_copy(update={"last_timestamp": END - timedelta(seconds=1), "count": 10})
            for e in original.events
        )
        sources.k8s = await stack.enter_async_context(
            FakeKubernetesConnector(
                KubernetesSnapshot(
                    deployments=original.deployments, pods=original.pods, events=later
                )
            )
        )
        assert await sources.collect(query) == before
        unstable = tuple(
            e.model_copy(update={"first_timestamp": None, "event_time": None}) for e in later
        )
        sources.k8s = await stack.enter_async_context(
            FakeKubernetesConnector(
                KubernetesSnapshot(
                    deployments=original.deployments, pods=original.pods, events=unstable
                )
            )
        )
        assert not any(e.source == "kubernetes" for e in (await sources.collect(query)).events)


@pytest.mark.asyncio
async def test_missing_bindings_and_empty_window_do_not_invent_events() -> None:
    missing = await snapshot("checkout-service")
    assert not missing.events and len(missing.missing_bindings) == 5
    async with AsyncExitStack() as stack:
        sources = await configured_sources(stack, Settings(APP_ENV="test"))
        old = await sources.collect(
            DeploymentQuery(
                service_name="payment-service",
                start=END + timedelta(seconds=1),
                end=END + timedelta(hours=1),
            )
        )
    assert not old.events
    with pytest.raises(RuntimeError, match="关闭"):
        await sources.git.list_changes(
            DeploymentQuery(service_name="payment-service", start=START, end=END)
        )


@pytest.mark.asyncio
async def test_wrong_service_or_duplicate_source_fact_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with AsyncExitStack() as stack:
        sources = await configured_sources(stack, Settings(APP_ENV="test"))
        query = DeploymentQuery(service_name="payment-service", start=START, end=END)
        originals = await sources.git.list_changes(query)
        monkeypatch.setattr(
            sources.git, "list_changes", AsyncMock(return_value=(originals[0], originals[0]))
        )
        with pytest.raises(ValidationError, match="重复"):
            await sources.collect(query)
        monkeypatch.setattr(
            sources.git,
            "list_changes",
            AsyncMock(
                return_value=(originals[0].model_copy(update={"service_name": "other-service"}),)
            ),
        )
        with pytest.raises(ValueError, match="服务或时间窗"):
            await sources.collect(query)


@pytest.mark.parametrize("value", [0, 2592001, True, "3600"])
def test_tool_time_window_is_bounded_and_strict(value: object) -> None:
    with pytest.raises(ValidationError):
        RecentChangesInput.model_validate(
            {"service_name": "payment-service", "lookback_seconds": value}
        )


@pytest.mark.parametrize(
    "change",
    [
        {"source_ref": ""},
        {"occurred_at": datetime(2026, 10, 1)},
        {"source": "argocd"},
        {"kind": "Config"},
        {"secret": "forbidden"},
    ],
)
def test_fact_requires_provenance_correct_source_and_aware_time(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChangeFact.model_validate(
            {
                "service_name": "payment-service",
                "source": "gitlab",
                "kind": "Commit",
                "source_ref": "gitlab:repo:sha",
                "occurred_at": START,
            }
            | change
        )


@pytest.mark.parametrize("provider", ["gitlab", "github"])
@pytest.mark.asyncio
async def test_git_fixed_get_endpoints_pagination_merge_and_boundary_filter(
    provider: Literal["gitlab", "github"],
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.host == "git.invalid"
        assert request.url.params["since"] == START.isoformat()
        assert request.url.params["until"] == END.isoformat()
        calls.append(request.url.params["page"])
        page = int(request.url.params["page"])
        if page == 4:
            return httpx.Response(200, json=[])
        timestamp = (
            (START + timedelta(minutes=5 * page)).isoformat() if page < 3 else END.isoformat()
        )
        parents = ["a", "b"] if page == 2 else ["a"]
        record = {
            "id": f"sha{page}",
            "sha": f"sha{page}",
            "committed_date": timestamp,
            "parent_ids": parents,
            "parents": [{"sha": p} for p in parents],
            "commit": {"committer": {"date": timestamp}},
            "message": "secret-must-not-copy",
        }
        return httpx.Response(
            200, json=[record], headers={"Link": '<https://evil.invalid>; rel="next"'}
        )

    async with HTTPGitConnector(
        GitConfig(
            base_url="https://git.invalid/api",
            provider=provider,
            page_size=1,
            services={"payment-service": "weipai/payment"},
        ),
        ReaderCredentials(connector="git", token=SecretStr("mock")),
        transport=httpx.MockTransport(handler),
    ) as reader:
        records = await reader.list_changes(
            DeploymentQuery(service_name="payment-service", start=START, end=END)
        )
    assert calls == ["1", "2", "3", "4"]
    assert [r.kind for r in records] == ["Commit", "Merge"]
    assert all("secret-must-not-copy" not in r.model_dump_json() for r in records)


@pytest.mark.parametrize("broken", ["duplicate", "limit", "naive", "parents"])
@pytest.mark.asyncio
async def test_git_rejects_incomplete_or_invalid_history(broken: str) -> None:
    record: dict[str, object] = {
        "id": "sha",
        "committed_date": START.isoformat(),
        "parent_ids": ["a"],
    }
    if broken == "naive":
        record["committed_date"] = "2026-10-01T01:00:00"
    if broken == "parents":
        record["parent_ids"] = None
    async with HTTPGitConnector(
        GitConfig(
            base_url="https://git.invalid/api",
            provider="gitlab",
            page_size=1,
            max_pages=1 if broken == "limit" else 2,
            services={"payment-service": "weipai/payment"},
        ),
        ReaderCredentials(connector="git", token=SecretStr("mock")),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[record])),
    ) as reader:
        with pytest.raises(ChangesResponseError):
            await reader.list_changes(
                DeploymentQuery(service_name="payment-service", start=START, end=END)
            )


@pytest.mark.parametrize("broken", [None, "service", "marker", "duplicate", "limit"])
@pytest.mark.asyncio
async def test_config_history_minimal_reference_and_pagination(broken: str | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/api/services/payment/versions"
        page = int(request.url.params["page"])
        data: dict[str, object] = {
            "service_name": "other" if broken == "service" else "payment-service",
            "versions": [
                {
                    "id": "id1" if broken == "duplicate" else f"id{page}",
                    "version": "v2.3.7",
                    "published_at": START.isoformat(),
                    "values": {"password": "never-copy"},
                }
            ],
            "next_page": page + 1 if page == 1 or broken == "limit" else None,
        }
        if broken == "marker":
            del data["next_page"]
        return httpx.Response(200, json=data)

    async with HTTPConfigCenterConnector(
        ConfigCenterConfig(
            base_url="https://config.invalid/api",
            services={"payment-service": "payment"},
            allowed_keys=("pool.size",),
            max_pages=2,
        ),
        ReaderCredentials(connector="config_center", token=SecretStr("mock")),
        transport=httpx.MockTransport(handler),
    ) as reader:
        query = DeploymentQuery(service_name="payment-service", start=START, end=END)
        if broken is not None:
            with pytest.raises(ChangesResponseError):
                await reader.list_changes(query)
        else:
            result = await reader.list_changes(query)
            assert len(result) == 2 and all("never-copy" not in r.model_dump_json() for r in result)


@pytest.mark.parametrize(
    "value",
    [
        TimelineInput(lookback_seconds=0),
        TimelineInput(activity_max_attempts=11),
        TimelineInput(service_name="../bad", end=END.isoformat()),
        TimelineInput(end="2026-10-01T01:00:00"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_workflow_input_is_non_retryable(value: TimelineInput) -> None:
    with pytest.raises(ApplicationError) as error:
        await ChangeTimelineWorkflow().run(value)
    assert error.value.non_retryable


@pytest.mark.asyncio
async def test_activity_does_not_persist_partial_collection_and_sanitizes_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(parse_database_url("postgresql+asyncpg://127.0.0.1/unused"))
    try:
        monkeypatch.setattr(
            activity_module,
            "configured_sources",
            AsyncMock(side_effect=RuntimeError("private-token")),
        )
        with pytest.raises(ApplicationError) as error:
            await TimelineActivities(database, Settings(APP_ENV="test")).collect(
                TimelineRequest("payment-service", START.isoformat(), END.isoformat())
            )
        assert "private-token" not in str(error.value)
        assert not error.value.non_retryable
        with pytest.raises(ApplicationError) as invalid:
            await TimelineActivities(database, Settings(APP_ENV="test")).collect(
                TimelineRequest("payment-service", "bad", "bad")
            )
        assert invalid.value.non_retryable
    finally:
        await database.dispose()
