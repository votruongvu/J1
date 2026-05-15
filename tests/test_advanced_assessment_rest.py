"""REST-level tests for the Advanced Assessment + Manual Actions
surfaces.

Pins the contracts the FE relies on:

  * ``POST /documents/{id}/advanced-assessment`` is operator-only —
    it NEVER runs as part of the default ``/assessment-plan`` path.
  * The endpoint returns a structured refusal (not a 4xx) when the
    deployment hasn't wired an LLM advanced-assessment service.
  * The endpoint persists a new ``AssessmentDecision`` with
    ``recommendationSource='llm_advanced_assessment'`` on success.
  * ``GET /documents/{id}/manual-actions`` returns the canonical
    vocabulary so the FE can render explicit operator buttons.
  * Refresh-enrich is marked deprecated in the OpenAPI schema; FE
    surfaces should not treat it as a primary action.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus
from j1.processing.assessment_decision import (
    JsonlAssessmentDecisionStore,
)
from j1.processing.execution_profile import ExecutionProfile
from j1.processing.execution_profile_policy import ExecutionProfilePolicy
from j1.processing.llm_advanced_assessment import (
    LLMAdvancedAssessmentInputs,
    LLMAdvancedAssessmentResult,
    LLMAdvancedAssessmentService,
    STATUS_OK,
    STATUS_REFUSED,
)
from j1.processing.llm_advanced_assessment_settings import (
    LLMAdvancedAssessmentSettings,
)


_NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def _headers() -> dict[str, str]:
    return {"X-Tenant-Id": "acme", "X-Project-Id": "alpha"}


def _envelope(payload):
    assert "data" in payload, payload
    return payload["data"]


def _stage_document(
    workspace, registry, ctx, *,
    document_id: str = "doc-adv",
    filename: str = "report.txt",
    content: bytes = b"hello adv assessment",
):
    raw_dir = workspace.raw(ctx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / filename).write_bytes(content)
    checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
    record = DocumentRecord(
        document_id=document_id, project=ctx,
        original_filename=filename, stored_filename=filename,
        mime_type="text/plain", file_size=len(content),
        checksum=checksum, status=ProcessingStatus.PENDING,
        created_at=_NOW,
    )
    registry.add(record)
    return record


# ---- Fixtures -------------------------------------------------------


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, audit_recorder,
):
    from j1.integration import (
        ApplicationFacade, CitationLookupService,
        DocumentIngestionService, EventPublisherService,
        RetrievalService, SourceLookupService,
    )
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=None,
        event_publisher=EventPublisherService(audit_recorder),
    )


@pytest.fixture
def started_jobs() -> list[tuple[str, str, str]]:
    return []


@pytest.fixture
def job_starter(started_jobs):
    async def starter(ctx, document_id, body):
        started_jobs.append(
            (ctx.project_id, document_id, body.compiler_kind),
        )
        starter.bodies.append(body)
        return f"job-{document_id}"
    starter.bodies = []
    return starter


@pytest.fixture
def decision_store(workspace):
    return JsonlAssessmentDecisionStore(workspace)


# Stub LLM service that always returns a usable OK result. Real
# behaviour is exercised in test_llm_advanced_assessment.py.

class _OkLLMService:
    def __init__(self):
        self.calls: list[LLMAdvancedAssessmentInputs] = []
    def run(self, inputs):
        self.calls.append(inputs)
        return LLMAdvancedAssessmentResult(
            status=STATUS_OK,
            document_complexity="complex",
            recommended_profile="deep_knowledge_index",
            confidence="high",
            detected_signals={"likely_tables": "likely"},
            recommended_next_steps=("run_domain_enrichment",),
            reasoning_summary=("LLM stub OK",),
            warnings=("This is an LLM-based estimate.",),
        )


class _RefusalLLMService:
    def run(self, inputs):
        return LLMAdvancedAssessmentResult(
            status=STATUS_REFUSED,
            refusal_reason="document_too_large",
            message=(
                "This document is too large for Advanced "
                "Assessment. Please choose a profile manually "
                "based on visible document complexity."
            ),
            warnings=(
                "This document is too large for Advanced "
                "Assessment. Please choose a profile manually "
                "based on visible document complexity.",
            ),
        )


def _make_app(
    facade, job_starter, workspace,
    *,
    decision_store=None,
    llm_service=None,
):
    from j1.integration.dto import ProcessingCapabilities
    caps = ProcessingCapabilities(
        compiler_kinds=frozenset({"mineru"}),
        default_compiler_kind="mineru",
    )
    return create_rest_api(
        facade, job_starter=job_starter, workspace=workspace,
        processing_capabilities=caps,
        assessment_decision_store=decision_store,
        llm_advanced_assessment_service=llm_service,
        execution_profile_policy=ExecutionProfilePolicy(
            default_profile=ExecutionProfile.STANDARD,
            allowed=frozenset(ExecutionProfile),
        ),
    )


# ---- Default ingest path stays lightweight -------------------------


def test_assessment_plan_does_not_invoke_llm_service(
    application_facade, job_starter, workspace, registry, ctx,
):
    """Regression: ``POST /documents/{id}/assessment-plan`` must NOT
    call the LLM service even when it's wired. The LLM is operator-
    triggered ONLY via ``/advanced-assessment``."""
    _stage_document(workspace, registry, ctx)
    llm = _OkLLMService()
    app = _make_app(
        application_facade, job_starter, workspace,
        llm_service=llm,
    )
    client = TestClient(app)
    resp = client.post(
        "/documents/doc-adv/assessment-plan",
        headers=_headers(),
    )
    assert resp.status_code == 200
    # The default path NEVER touches the LLM service.
    assert llm.calls == []
    # And the recommendation source is NOT llm_advanced_assessment.
    data = _envelope(resp.json())
    assert data["recommendationSource"] != "llm_advanced_assessment"


# ---- Advanced Assessment endpoint ---------------------------------


def test_advanced_assessment_returns_structured_refusal_without_llm(
    application_facade, job_starter, workspace, registry, ctx,
):
    """No LLM service wired → endpoint returns ``status='refused'``
    payload (NOT a 4xx). The FE renders a polite manual-selection
    prompt instead of erroring."""
    _stage_document(workspace, registry, ctx)
    app = _make_app(
        application_facade, job_starter, workspace,
        llm_service=None,
    )
    client = TestClient(app)
    resp = client.post(
        "/documents/doc-adv/advanced-assessment",
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = _envelope(resp.json())
    assert data["assessmentDecisionId"] is None
    assert data["result"]["status"] == STATUS_REFUSED
    assert data["result"]["refusalReason"] == "llm_unavailable"


def test_advanced_assessment_runs_llm_and_persists_decision(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """Happy path: LLM returns an OK result, the endpoint persists
    a new AssessmentDecision with ``recommendationSource='llm_advanced_assessment'``,
    and returns the new decision id so the FE can thread it through
    the upload."""
    _stage_document(workspace, registry, ctx)
    llm = _OkLLMService()
    app = _make_app(
        application_facade, job_starter, workspace,
        decision_store=decision_store, llm_service=llm,
    )
    client = TestClient(app)
    resp = client.post(
        "/documents/doc-adv/advanced-assessment",
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = _envelope(resp.json())
    decision_id = data["assessmentDecisionId"]
    assert decision_id is not None
    assert data["result"]["status"] == STATUS_OK
    assert data["result"]["recommendedProfile"] == "deep_knowledge_index"
    assert (
        "run_domain_enrichment"
        in data["result"]["recommendedNextSteps"]
    )
    # The decision was persisted to the store.
    persisted = decision_store.get(ctx, decision_id)
    assert persisted is not None
    assert persisted.document_id == "doc-adv"
    assert persisted.recommendation_source == "llm_advanced_assessment"
    assert persisted.effective_profile == "advanced"
    assert persisted.recommended_next_steps == (
        "run_domain_enrichment",
    )
    # LLM was called exactly once.
    assert len(llm.calls) == 1
    assert llm.calls[0].document_id == "doc-adv"


def test_advanced_assessment_refusal_does_not_persist_decision(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """A refusal must NOT persist a new decision — the picker keeps
    whatever recommendation it already had."""
    _stage_document(workspace, registry, ctx)
    app = _make_app(
        application_facade, job_starter, workspace,
        decision_store=decision_store,
        llm_service=_RefusalLLMService(),
    )
    client = TestClient(app)
    resp = client.post(
        "/documents/doc-adv/advanced-assessment",
        headers=_headers(),
    )
    data = _envelope(resp.json())
    assert data["assessmentDecisionId"] is None
    assert data["result"]["status"] == STATUS_REFUSED
    assert "manually" in data["result"]["message"].lower()


def test_advanced_assessment_unknown_document_returns_404(
    application_facade, job_starter, workspace,
):
    app = _make_app(application_facade, job_starter, workspace)
    client = TestClient(app)
    resp = client.post(
        "/documents/ghost/advanced-assessment",
        headers=_headers(),
    )
    assert resp.status_code == 404


# ---- Manual Actions endpoint ---------------------------------------


def test_manual_actions_endpoint_lists_canonical_vocabulary(
    application_facade, job_starter, workspace, registry, ctx,
):
    _stage_document(workspace, registry, ctx)
    app = _make_app(application_facade, job_starter, workspace)
    client = TestClient(app)
    resp = client.get(
        "/documents/doc-adv/manual-actions",
        headers=_headers(),
    )
    assert resp.status_code == 200
    data = _envelope(resp.json())
    ids = [a["id"] for a in data["actions"]]
    # Canonical set — pinned so a new action that ships without an
    # FE handler breaks at test time, not in the picker.
    assert "run_llm_advanced_assessment" in ids
    assert "run_domain_enrichment" in ids
    assert "build_knowledge_memory" in ids
    assert "normalize_entities" in ids
    assert "build_deep_knowledge_index" in ids
    # Status field present + sane.
    for action in data["actions"]:
        assert action["status"] in {
            "available", "not_implemented", "disabled",
        }
        assert action["costNote"]
        assert action["label"]


def test_manual_actions_unknown_document_returns_404(
    application_facade, job_starter, workspace,
):
    app = _make_app(application_facade, job_starter, workspace)
    client = TestClient(app)
    resp = client.get(
        "/documents/ghost/manual-actions",
        headers=_headers(),
    )
    assert resp.status_code == 404


# ---- Refresh-enrich deprecation -----------------------------------


def test_refresh_enrich_marked_deprecated_in_openapi(
    application_facade, job_starter, workspace,
):
    """FE surfaces should not render Refresh Enrich as the primary
    action in the new flow. The OpenAPI schema flags it deprecated
    so any FE that generates clients from the schema gets a clear
    signal."""
    app = _make_app(application_facade, job_starter, workspace)
    schema = app.openapi()
    path = schema["paths"][
        "/ingestion-runs/{run_id}/refresh-enrichment"
    ]
    op = path.get("post")
    assert op is not None
    assert op.get("deprecated") is True
    # The summary still mentions "Deprecated" so operators reading
    # the docs immediately understand.
    assert "Deprecated" in op["summary"]
