"""十三章节、严格证据引用、草稿权限及离线生成。"""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.investigation import AgentConclusion, EvidenceClaim
from app.connectors.observability.fake import SAMPLE_END
from app.learning.engine import compose
from app.learning.models import SECTIONS, IncidentSearch, PostmortemDraft
from app.ledger.models import Evidence
from app.runbooks.schemas import AutomationLevel, RunbookMaturity
from app.tasks.workflow import validate_workflow_input
from app.tasks.workflow_models import WorkflowInput
from app.tools.models import ToolModel

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def sample() -> PostmortemDraft:
    return compose(uuid4(), "支付 5xx", "payment-service", [])[0]


@pytest.mark.parametrize("missing", range(13))
def test_every_design_section_is_required(missing: int) -> None:
    value = sample().model_dump(mode="json")
    value["sections"].pop(missing)
    with pytest.raises(ValidationError):
        PostmortemDraft.model_validate_json(__import__("json").dumps(value))


def test_section_order_duplicate_and_uncited_claims_rejected() -> None:
    import json

    value = sample().model_dump(mode="json")
    value["sections"][0]["title"] = "自行添加的章节"
    with pytest.raises(ValidationError):
        PostmortemDraft.model_validate_json(json.dumps(value))
    value = sample().model_dump(mode="json")
    value["sections"][0]["conclusions"][0]["evidence_ids"] = []
    with pytest.raises(ValidationError):
        PostmortemDraft.model_validate_json(json.dumps(value))


def test_unknown_facts_are_explicit_and_draft_has_no_execution_authority() -> None:
    draft, runbook = compose(uuid4(), "支付 5xx", "payment-service", [])
    assert tuple(s.title for s in draft.sections) == SECTIONS
    assert "尚未确认" in draft.sections[4].conclusions[0].statement
    assert "没有独立验证" in draft.sections[6].conclusions[0].statement
    assert runbook.maturity is RunbookMaturity.DRAFT
    assert runbook.automation_level is AutomationLevel.MANUAL
    assert runbook.success_count == runbook.failure_count == 0


def test_root_cause_reuses_accepted_ai_conclusion_and_actual_evidence() -> None:
    task_id, fact_id, conclusion_id = uuid4(), uuid4(), uuid4()
    fact = Evidence(
        id=fact_id,
        task_id=task_id,
        source_tool="query_logs",
        parameters={"service_name": "payment-service", "start": "a", "end": "b"},
        result_snapshot={},
        collected_at=SAMPLE_END,
    )
    claim = EvidenceClaim(statement="连接池 50→500 导致连接耗尽", evidence_ids=(fact_id,))
    conclusion = Evidence(
        id=conclusion_id,
        task_id=task_id,
        source_tool="agent.conclusion",
        parameters={},
        result_snapshot=AgentConclusion(
            root_cause=claim, findings=(claim,), confidence=0.7
        ).model_dump(mode="json"),
        collected_at=SAMPLE_END,
    )
    draft, runbook = compose(uuid4(), "支付故障", "payment-service", [fact, conclusion])
    assert draft.sections[4].conclusions[0].statement == claim.statement
    assert draft.sections[4].conclusions[0].evidence_ids == (conclusion_id, fact_id)
    assert runbook.diagnostic_steps[0].parameters["start"] == "$start"
    assert runbook.diagnostic_steps[0].parameters["service_name"] == "$service_name"


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_model_cannot_smuggle_postmortem_flag(value: object) -> None:
    with pytest.raises(ValueError):
        validate_workflow_input(replace(WorkflowInput(str(uuid4())), postmortem_enabled=value))  # type: ignore[arg-type]


def test_search_requires_a_real_query() -> None:
    with pytest.raises(ValidationError):
        IncidentSearch(query="   ")
    assert issubclass(PostmortemDraft, ToolModel)


def test_verification_resource_targets_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv(
        "VERIFICATION_CONFIG",
        '{"resources_by_service":{"payment-service":[{"product":"rds","region_id":"cn-hangzhou","resource_id":"rm-payment","healthy_status":"Running"}]}}',
    )
    targets = Settings().verification_config.resources_by_service["payment-service"]
    assert len(targets) == 1 and targets[0].resource_id == "rm-payment"
