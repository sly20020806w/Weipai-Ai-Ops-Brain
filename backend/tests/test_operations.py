"""认知与运营 API 离线契约：完整 OpenAPI、鉴权及边界输入。"""

from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from app.api.main import create_app
from app.api.operations import get_catalog, get_operations
from app.config import Settings
from app.tasks.operations_models import KnowledgeInput, RunbookInput
from tests.test_console import authenticated_app

pytestmark = pytest.mark.usefixtures("forbid_llm_network")
CENTERS = ("releases", "tickets", "inspections", "war-rooms", "architecture-reviews", "automations")
LISTS = [
    "/api/services",
    "/api/context-graph/nodes",
    "/api/context-graph/edges",
    "/api/changes",
    "/api/runbooks",
    "/api/knowledge",
    "/api/risks",
    "/api/audits",
    *[f"/api/{center}" for center in CENTERS],
]
DETAILS = [
    "/api/services/{id}",
    "/api/services/{id}/dependencies",
    "/api/context-graph/nodes/{id}",
    "/api/context-graph/edges/{id}",
    "/api/changes/{id}",
    "/api/runbooks/{id}",
    "/api/knowledge/{id}",
    "/api/risks/{id}",
    "/api/audits/{id}",
    *[f"/api/{center}/{{id}}" for center in CENTERS],
]
WRITES = [
    (method, path)
    for name in ("knowledge", "runbooks")
    for method, path in (
        ("POST", f"/api/{name}"),
        ("PUT", f"/api/{name}/{{id}}"),
        ("DELETE", f"/api/{name}/{{id}}"),
    )
]


def runbook_body() -> dict[str, object]:
    return {
        "name": f"payment-inspection-{uuid4().hex}",
        "description": "支付健康检查",
        "source": "本人 SOP",
        "applicability_conditions": [
            {"field": "service_name", "operator": "equals", "value": "payment-service"}
        ],
        "exclusion_conditions": [],
        "diagnostic_steps": [
            {
                "description": "读取服务图",
                "tool_name": "get_service_context",
                "parameters": {"service_name": "payment-service"},
                "risk_level": "L0",
            }
        ],
        "handling_steps": [{"description": "确认异常后审批回滚", "risk_level": "L3"}],
        "risk_level": "L3",
        "rollback_plan": "审批后恢复原版本",
        "verification_steps": ["独立核验业务指标"],
    }


def test_openapi_all_groups_typed_and_protected() -> None:
    schema = create_app(Settings(APP_ENV="test")).openapi()
    actual = schema["paths"]
    for path in LISTS + ["/api/metrics"]:
        assert (
            path in actual
            and actual[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        )
    for name in ("knowledge", "runbooks"):
        assert "post" in actual[f"/api/{name}"]
        detail = next(p for p in actual if p.startswith(f"/api/{name}/"))
        assert {"get", "put", "delete"} <= actual[detail].keys()
    ids = []
    for path, methods in actual.items():
        if path.startswith("/api/") and not path.startswith("/api/auth/"):
            for method, operation in methods.items():
                ids.append(operation["operationId"])
                assert operation["security"] == [{"SessionCookie": []}]
                if method in {"post", "put", "delete"}:
                    assert any(
                        p["name"] == "X-CSRF-Token" and p["required"]
                        for p in operation["parameters"]
                    )
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [("GET", p) for p in LISTS + DETAILS + ["/api/metrics", "/api/metrics/rca_hit_rate"]] + WRITES,
)
async def test_every_operation_requires_login(method: str, path: str) -> None:
    app = create_app(Settings(APP_ENV="test"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.request(method, path.format(id=uuid4()))).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", LISTS)
async def test_every_list_bounds(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    app, database = authenticated_app(monkeypatch)
    app.dependency_overrides[get_operations] = object
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Cookie": "ops_session=test"},
        ) as client:
            for query in ("limit=0", "limit=101", "offset=-1", "limit=x"):
                assert (await client.get(f"{path}?{query}")).status_code == 422
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", WRITES)
async def test_catalog_writes_csrf_and_forged_actor(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    app, database = authenticated_app(monkeypatch)
    app.dependency_overrides[get_catalog] = object
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Cookie": "ops_session=test"},
        ) as client:
            target = path.format(id=uuid4())
            assert (await client.request(method, target)).status_code == 403
            assert (
                await client.request(
                    method,
                    target,
                    headers={"X-CSRF-Token": "csrf", "Origin": "https://foreign.invalid"},
                )
            ).status_code == 403
            if method != "DELETE":
                assert (
                    await client.request(
                        method, target, json={"actor": "forged"}, headers={"X-CSRF-Token": "csrf"}
                    )
                ).status_code == 422
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/changes", "/api/audits", "/api/metrics"])
async def test_utc_window_validation(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    app, database = authenticated_app(monkeypatch)
    app.dependency_overrides[get_operations] = object
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Cookie": "ops_session=test"},
        ) as client:
            for query in (
                {"start": "2026-10-08T00:00:00"},
                {"start": "2026-10-09T00:00:00Z", "end": "2026-10-08T00:00:00Z"},
            ):
                assert (await client.get(path, params=query)).status_code == 422
    finally:
        await database.dispose()


def test_catalog_input_strictness_and_managed_fields() -> None:
    RunbookInput.model_validate(runbook_body())
    for field, value in (
        ("maturity", "self_healing"),
        ("success_count", 100),
        ("actor", "forged"),
        ("risk_level", True),
    ):
        with pytest.raises(ValidationError):
            RunbookInput.model_validate({**runbook_body(), field: value})
    for field in RunbookInput.model_fields:
        body = runbook_body()
        body.pop(field)
        with pytest.raises(ValidationError):
            RunbookInput.model_validate(body)
    with pytest.raises(ValidationError):
        KnowledgeInput.model_validate({"kind": "standard", "content": "  ", "source": "本人"})
