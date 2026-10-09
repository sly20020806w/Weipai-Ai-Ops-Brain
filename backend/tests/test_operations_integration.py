"""本机独立 PostgreSQL：各中心列表/详情、编辑原子性与审计保护。"""

import os
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.agent.client import GatewayResponseError
from app.config import Settings
from app.db.base import utc_now
from app.db.session import Database
from app.graph.changes.models import ChangeEvent
from app.graph.service import GraphService
from app.knowledge.models import KnowledgeEntry
from app.ledger.models import AppendOnlyViolation, CatalogAudit
from app.ledger.service import LedgerService
from app.runbooks.maturity_scenario import human_review
from app.runbooks.models import Runbook
from app.runbooks.schemas import RunbookView
from app.tasks.catalog_service import CatalogService
from app.tasks.inspection.models import RiskEntry
from app.tasks.states import TaskSource
from app.triggers.schemas import NormalizedEvent
from app.triggers.service import EventService
from tests.test_console_integration import api, http
from tests.test_operations import CENTERS, runbook_body
from tests.test_reviewer_integration import database, migrated_schema

__all__ = ["api", "http", "database", "migrated_schema"]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="运行 check-operations.ps1"),
]


async def event_for(database: Database, center: str) -> tuple[str, UUID, UUID]:
    service = f"service-{uuid4().hex[:12]}"
    origins = {
        "releases": ("ops_platform", TaskSource.RELEASE, "release:"),
        "tickets": ("ops_platform", TaskSource.TICKET, "ticket:"),
        "inspections": ("schedule", TaskSource.SCHEDULE, "workday-inspection:"),
        "war-rooms": ("manual", TaskSource.HUMAN, "war-room:"),
        "architecture-reviews": ("manual", TaskSource.HUMAN, "architecture-review:"),
        "automations": ("learning", TaskSource.AI, "automation:"),
    }
    origin, source, prefix = origins[center]
    async with database.session() as session, session.begin():
        receipt = (
            await EventService(session).accept(
                [
                    NormalizedEvent(
                        origin=origin,
                        source=source,
                        external_id=prefix + str(uuid4()),
                        service_name=service,
                        title="接口 Fake 查询样例",
                        occurred_at=utc_now(),
                    )
                ]
            )
        )[0]
        evidence = await LedgerService(session).append_evidence(
            task_id=UUID(receipt.task_id),
            source_tool="console.fake.fixture",
            parameters={},
            result_snapshot={"service_name": service, "sample": center},
        )
        return service, UUID(receipt.task_id), evidence.id


@pytest.mark.parametrize("center", CENTERS)
async def test_centers_lists_details_filters_missing_wrong_kind(
    http: httpx.AsyncClient, database: Database, center: str
) -> None:
    service, task_id, evidence_id = await event_for(database, center)
    response = await http.get(f"/api/{center}", params={"service_name": service})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["task"]["id"] == str(task_id)
    detail = await http.get(f"/api/{center}/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["event"]["service_name"] == service
    assert any(e["id"] == str(evidence_id) for e in detail.json()["evidence"])
    assert (await http.get(f"/api/evidence/{evidence_id}")).json()["result_snapshot"][
        "sample"
    ] == center
    assert (await http.get(f"/api/{center}", params={"service_name": service, "offset": 1})).json()[
        "items"
    ] == []
    assert (
        await http.get(f"/api/{center}", params={"service_name": service, "status": "CLOSED"})
    ).json()["total"] == 0
    assert (await http.get(f"/api/{center}/{uuid4()}")).status_code == 404
    assert (await http.get(f"/api/{center}/invalid")).status_code == 422
    other = "tickets" if center != "tickets" else "releases"
    assert (await http.get(f"/api/{other}/{task_id}")).status_code == 404


async def test_graph_changes_risks_all_lists_details_and_utc(
    http: httpx.AsyncClient, database: Database
) -> None:
    service, task_id, evidence_id = await event_for(database, "inspections")
    now = utc_now()
    async with database.session() as session, session.begin():
        graph = GraphService(session)
        root = await graph.upsert_node(kind="service", external_id=service, name="样例支付")
        target = await graph.upsert_node(
            kind="service", external_id=service + "-upstream", name="样例上游"
        )
        edge = await graph.upsert_edge(
            from_node_id=target.id,
            to_node_id=root.id,
            relation="calls",
            source="fake-arms",
            confidence=0.99,
            observed_at=now - timedelta(seconds=30),
        )
        change = ChangeEvent(
            service_name=service,
            source="gitlab",
            kind="Commit",
            source_ref=f"fake://git/{uuid4()}",
            occurred_at=now,
            revision="v2.3.7",
        )
        risk = RiskEntry(
            risk_key=uuid4().hex * 2,
            service_name=service,
            check_id="no-pdb",
            resource="deployment/payment",
            category="stability",
            outcome="abnormal",
            active=True,
            episode=1,
            first_seen=now,
            last_seen=now,
            opening_evidence_id=evidence_id,
            latest_evidence_id=evidence_id,
        )
        session.add_all([change, risk])
        await session.flush()
        ids = {
            "/api/context-graph/nodes": root.id,
            "/api/context-graph/edges": edge.id,
            "/api/changes": change.id,
            "/api/risks": risk.id,
        }

    async def listed_identity(path: str, identity: UUID, params: dict[str, str]) -> bool:
        first = await http.get(path, params=params)
        assert first.status_code == 200
        data = first.json()
        if any(row["id"] == str(identity) for row in data["items"]):
            return True
        for offset in range(data["limit"], data["total"], data["limit"]):
            page = await http.get(path, params={**params, "offset": str(offset)})
            assert page.status_code == 200
            if any(row["id"] == str(identity) for row in page.json()["items"]):
                return True
        return False

    assert await listed_identity("/api/services", root.id, {})
    for path, identity in ids.items():
        params = (
            {"service_name": service}
            if path in {"/api/changes", "/api/risks"}
            else {"node_id": str(root.id)}
            if path.endswith("edges")
            else {}
        )
        response = await http.get(path, params=params)
        assert response.status_code == 200
        assert await listed_identity(path, identity, params)
        assert (await http.get(f"{path}/{identity}")).status_code == 200
        assert (await http.get(f"{path}/{uuid4()}")).status_code == 404
        assert (await http.get(f"{path}/invalid")).status_code == 422
    context = (await http.get(f"/api/services/{service}")).json()
    assert context["edges"][0]["freshness_seconds"] >= 30
    assert context["edges"][0]["confidence"] == 0.99
    assert context["edges"][0]["source"] == "fake-arms"
    assert context["edges"][0]["last_seen"].endswith("Z")
    assert (
        len(
            (
                await http.get(
                    f"/api/services/{service}/dependencies", params={"direction": "upstream"}
                )
            ).json()["nodes"]
        )
        == 2
    )
    assert (
        len(
            (
                await http.get(
                    f"/api/services/{service}/dependencies", params={"direction": "downstream"}
                )
            ).json()["nodes"]
        )
        == 1
    )
    assert (await http.get(f"/api/services/{service}?hops=5")).status_code == 422
    assert (await http.get("/api/services/unknown")).status_code == 404
    assert (
        await http.get(
            f"/api/changes?service_name={service}",
            params={"start": now.isoformat(), "end": (now + timedelta(seconds=1)).isoformat()},
        )
    ).json()["total"] == 1
    assert (
        await http.get(
            "/api/changes",
            params={
                "service_name": service,
                "start": (now - timedelta(seconds=1)).isoformat(),
                "end": now.isoformat(),
            },
        )
    ).json()["total"] == 0
    assert (
        await http.get("/api/risks", params={"service_name": service, "active": "false"})
    ).json()["total"] == 0
    assert (await http.get("/api/risks", params={"category": "invalid"})).status_code == 422
    assert (
        await http.get("/api/context-graph/edges", params={"node_id": str(uuid4())})
    ).status_code == 404
    assert task_id is not None


@pytest.mark.parametrize("name", ["knowledge", "runbooks"])
async def test_catalog_crud_real_vector_and_actor_audit(
    http: httpx.AsyncClient, database: Database, name: str
) -> None:
    body = (
        {"kind": "business_rule", "content": "payment 支付规则", "source": "本人业务规则"}
        if name == "knowledge"
        else runbook_body()
    )
    response = await http.post(f"/api/{name}", json=body)
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    assert response.json()["embedding_dimensions"] == 4
    assert (await http.get(f"/api/{name}/{identity}")).json() == response.json()
    page = (await http.get(f"/api/{name}")).json()
    assert any(e["id"] == identity for e in page["items"])
    model = KnowledgeEntry if name == "knowledge" else Runbook
    async with database.session() as session:
        before = await session.get(model, UUID(identity))
        assert isinstance(before, (KnowledgeEntry, Runbook))
        old_vector = list(before.embedding)
    changed = (
        {**body, "content": "network 网络规范"}
        if name == "knowledge"
        else {**body, "description": "network 网络检查"}
    )
    updated = await http.put(f"/api/{name}/{identity}", json=changed)
    assert updated.status_code == 200, updated.text
    async with database.session() as session:
        current = await session.get(model, UUID(identity))
        assert isinstance(current, (KnowledgeEntry, Runbook))
        assert list(current.embedding) != old_vector
    if name == "runbooks":
        assert updated.json()["content_version"] == 2
        assert updated.json()["maturity"] == "draft" and updated.json()["success_count"] == 0
        assert (
            await http.post("/api/runbooks", json={**body, "maturity": "self_healing"})
        ).status_code == 422
    assert (await http.delete(f"/api/{name}/{identity}")).status_code == 204
    assert (await http.get(f"/api/{name}/{identity}")).status_code == 404
    assert (await http.put(f"/api/{name}/{uuid4()}", json=body)).status_code == 404
    assert (await http.delete(f"/api/{name}/{uuid4()}")).status_code == 404
    assert (await http.post(f"/api/{name}", json={})).status_code == 422
    assert (await http.get(f"/api/{name}/invalid")).status_code == 422
    records = (
        await http.get("/api/audits", params={"actor": "local-owner", "event_type": "catalog_edit"})
    ).json()
    relevant = [a for a in records["items"] if a["details"].get("record_id") == identity]
    assert {a["operation"] for a in relevant} == {
        f"{name}.{v}" for v in ("create", "update", "delete")
    }
    assert all(a["task_id"] is None and a["occurred_at"].endswith("Z") for a in relevant)
    for audit in relevant:
        assert (await http.get("/api/audits/" + audit["id"])).json() == audit


@pytest.mark.parametrize("name", ["knowledge", "runbooks"])
async def test_audit_failure_rolls_back_catalog_change(
    http: httpx.AsyncClient, database: Database, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    body = (
        {"kind": "standard", "content": "payment 支付", "source": "本人"}
        if name == "knowledge"
        else runbook_body()
    )
    created = await http.post(f"/api/{name}", json=body)
    identity = created.json()["id"]

    async def failed(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("private credentials must not leak")

    monkeypatch.setattr(CatalogService, "audit", failed)
    updated_body = (
        {**body, "content": "network"}
        if name == "knowledge"
        else {**body, "description": "network"}
    )
    for method, payload in (("PUT", updated_body), ("DELETE", None)):
        result = await http.request(method, f"/api/{name}/{identity}", json=payload)
        assert result.status_code == 503 and "private" not in result.text
        assert (await http.get(f"/api/{name}/{identity}")).json() == created.json()


async def test_embedding_failure_not_committed_and_delete_offline(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.tasks.catalog_service as module

    body = {"kind": "standard", "content": "payment", "source": "本人"}
    identity = (await http.post("/api/knowledge", json=body)).json()["id"]

    def unavailable(*args: object) -> None:
        raise GatewayResponseError("private upstream response")

    monkeypatch.setattr(module, "embedding_client", unavailable)
    result = await http.put(f"/api/knowledge/{identity}", json={**body, "content": "network"})
    assert result.status_code == 503 and "private" not in result.text
    assert (await http.get(f"/api/knowledge/{identity}")).json()["content"] == "payment"
    assert (await http.delete(f"/api/knowledge/{identity}")).status_code == 204


async def test_metrics_and_audit_missing_filters_window(
    http: httpx.AsyncClient, database: Database
) -> None:
    service, task_id, _ = await event_for(database, "tickets")
    report = await http.get("/api/metrics")
    assert report.status_code == 200 and len(report.json()["metrics"]) == 10
    for metric in report.json()["metrics"]:
        assert (await http.get("/api/metrics/" + metric["name"])).json() == metric
    assert (await http.get("/api/metrics/unknown")).status_code == 404
    own = (await http.get("/api/audits", params={"task_id": str(task_id)})).json()
    assert own["total"] == 1 and own["items"][0]["task_id"] == str(task_id)
    assert (await http.get("/api/audits", params={"actor": "absent"})).json()["total"] == 0
    assert (await http.get("/api/audits", params={"task_id": str(uuid4())})).status_code == 404
    assert (await http.get("/api/audits/" + str(uuid4()))).status_code == 404
    assert (await http.get("/api/audits/invalid")).status_code == 422
    assert service


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE catalog_audit_log SET actor='forged'",
        "DELETE FROM catalog_audit_log",
        "TRUNCATE catalog_audit_log",
    ],
)
async def test_catalog_audit_raw_mutations_blocked(database: Database, sql: str) -> None:
    async with database.session() as session, session.begin():
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text(sql))
        await session.rollback()


async def test_catalog_audit_orm_protection(database: Database) -> None:
    async with database.session() as session, session.begin():
        row = CatalogAudit(
            actor="owner", operation="knowledge.create", details={"record_id": str(uuid4())}
        )
        session.add(row)
        await session.flush()
        identity = row.id
    async with database.session() as session, session.begin():
        record = await session.get(CatalogAudit, identity)
        assert record is not None
        record.actor = "forged"
        with pytest.raises(AppendOnlyViolation):
            await session.flush()
        await session.rollback()


async def test_runbook_content_edit_revokes_actual_review(
    http: httpx.AsyncClient, database: Database
) -> None:
    body = runbook_body()
    created = await http.post("/api/runbooks", json=body)
    assert created.status_code == 201, created.text
    original = RunbookView.model_validate_json(created.text)
    reviewed = await human_review(database, Settings(APP_ENV="test"), original)
    assert reviewed.maturity.value == "reviewed"
    unchanged = await http.put(f"/api/runbooks/{original.id}", json=body)
    assert unchanged.status_code == 200 and unchanged.json()["maturity"] == "reviewed"
    changed = await http.put(
        f"/api/runbooks/{original.id}", json={**body, "description": "修改后的支付检查"}
    )
    assert changed.status_code == 200
    assert changed.json()["maturity"] == "draft"
    assert changed.json()["automation_level"] == "manual"
    assert changed.json()["content_version"] == reviewed.content_version + 1
    assert changed.json()["confidence"] == 0.5


async def test_nonempty_catalog_audit_downgrade_refused(database: Database) -> None:
    import subprocess

    from tests.database_support import migrate

    async with database.session() as session, session.begin():
        session.add(
            CatalogAudit(
                actor="owner", operation="knowledge.delete", details={"record_id": str(uuid4())}
            )
        )
    with pytest.raises(subprocess.CalledProcessError):
        migrate("downgrade", "0015_single_user_auth")
    async with database.session() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0016_catalog_audit"
        )


async def test_runbook_duplicate_name_is_conflict_without_extra_audit(
    http: httpx.AsyncClient,
) -> None:
    body = runbook_body()
    created = await http.post("/api/runbooks", json=body)
    assert created.status_code == 201
    before = (await http.get("/api/audits?event_type=catalog_edit")).json()["total"]
    duplicate = await http.post("/api/runbooks", json=body)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Runbook 名称已存在"
    assert (await http.get("/api/audits?event_type=catalog_edit")).json()["total"] == before
