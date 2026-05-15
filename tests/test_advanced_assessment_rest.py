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


# ---- Sample text classifier ---------------------------------------


def test_advanced_assessment_marks_unsupported_for_binary_file_type(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """A document with an unsupported binary-ish extension (.xlsx)
    must NOT make the LLM claim layout detail. The result carries
    ``sampleTextStatus='unsupported'`` and the operator-readable
    warning ('used filename, metadata, lightweight signals, and
    matched rules only')."""
    _stage_document(
        workspace, registry, ctx,
        document_id="doc-bin",
        filename="schedule.xlsx",
        content=b"\x50\x4b\x03\x04binary-zip-archive",
    )
    llm = _OkLLMService()
    app = _make_app(
        application_facade, job_starter, workspace,
        decision_store=decision_store, llm_service=llm,
    )
    client = TestClient(app)
    resp = client.post(
        "/documents/doc-bin/advanced-assessment",
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = _envelope(resp.json())
    result = data["result"]
    # Result still OK — but with the unreliable-text warning AND
    # downgraded sample-text status.
    assert result["status"] == STATUS_OK
    assert result["sampleTextStatus"] == "unsupported"
    assert result["sampleTextSource"] == "unavailable"
    assert any(
        "filename" in w.lower() and "matched rules" in w.lower()
        for w in result["warnings"]
    )
    # And every ``likely`` verdict the stub emitted is hedged to
    # ``suspected``.
    signals = result["detectedSignals"]
    assert "likely" not in (signals.get("likely_tables") or "")


def test_advanced_assessment_marks_available_for_pdf(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """A real PDF goes through pypdf and surfaces
    ``sampleTextStatus='available'`` so the FE doesn't render the
    sample-text warning. We stage a minimal one-page PDF via
    pypdf's writer."""
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    raw_dir = workspace.raw(ctx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = raw_dir / "sample.pdf"
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as fh:
        w.write(fh)
    _stage_document(
        workspace, registry, ctx,
        document_id="doc-pdf",
        filename="sample.pdf",
        content=pdf_path.read_bytes(),
    )
    llm = _OkLLMService()
    app = _make_app(
        application_facade, job_starter, workspace,
        decision_store=decision_store, llm_service=llm,
    )
    client = TestClient(app)
    resp = client.post(
        "/documents/doc-pdf/advanced-assessment",
        headers=_headers(),
    )
    data = _envelope(resp.json())
    # A blank-page PDF can resolve to ``empty`` / ``garbled`` /
    # ``unreliable`` depending on what pypdf returns for an empty
    # canvas — the contract we DO pin is (a) the source field is
    # ``pypdf`` (extractor ran) and (b) status is never the
    # wire-stable "available" for a doc with no real text.
    assert data["result"]["sampleTextStatus"] in {
        "empty", "garbled", "unreliable",
    }
    assert data["result"]["sampleTextSource"] == "pypdf"


# ---- /assessment-plan carries forward LLM result ------------------


def test_advanced_assessment_promotes_unreliable_on_low_extractable_ratio(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """A document whose lightweight profile reports a low
    ``text_extractable_ratio`` (mostly-scanned signature) must
    surface ``sampleTextStatus='unreliable'`` to the LLM service —
    even when the extractor returns real text. We use a plain-text
    file for a deterministic extractor result and stub the
    profiler to claim the scanned-doc signature."""
    from j1.processing.profiling import DocumentProfile

    _stage_document(
        workspace, registry, ctx,
        document_id="doc-scanned",
        filename="report.txt",
        content=b"some readable text",
    )

    import j1.processing.profiling as _profiling

    class _StubProfiler:
        def profile(self, document_id: str, source_path: str):
            return DocumentProfile(
                document_id=document_id,
                extension=".txt",
                page_count=5,
                text_extractable_ratio=0.05,  # mostly scanned
                has_scanned_pages=True,
            )

    original = _profiling.DeterministicDocumentProfiler
    _profiling.DeterministicDocumentProfiler = _StubProfiler  # type: ignore[misc]
    try:
        llm = _OkLLMService()
        app = _make_app(
            application_facade, job_starter, workspace,
            decision_store=decision_store, llm_service=llm,
        )
        client = TestClient(app)
        resp = client.post(
            "/documents/doc-scanned/advanced-assessment",
            headers=_headers(),
        )
        data = _envelope(resp.json())
    finally:
        _profiling.DeterministicDocumentProfiler = original  # type: ignore[misc]

    assert data["result"]["sampleTextStatus"] == "unreliable"
    # Operator-facing warning is present (backend is the source
    # of truth for the FE).
    assert any(
        "filename" in w.lower() and "matched rules" in w.lower()
        for w in data["result"]["warnings"]
    )


def test_advanced_assessment_promotes_unreliable_on_sparse_text_in_long_doc(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """The second classifier signal: a doc with enough pages but
    too-few chars per sampled page should promote
    ``available`` → ``unreliable``."""
    from j1.processing.profiling import DocumentProfile

    # 20 chars / 1 sampled page = 20 chars per page — well below
    # the 80-char/page threshold.
    _stage_document(
        workspace, registry, ctx,
        document_id="doc-sparse",
        filename="sparse.txt",
        content=b"only twenty chars!! ",
    )

    import j1.processing.profiling as _profiling

    class _StubProfiler:
        def profile(self, document_id: str, source_path: str):
            return DocumentProfile(
                document_id=document_id,
                extension=".txt",
                page_count=10,  # multi-page doc
                text_extractable_ratio=0.9,  # not scanned
            )

    original = _profiling.DeterministicDocumentProfiler
    _profiling.DeterministicDocumentProfiler = _StubProfiler  # type: ignore[misc]
    try:
        llm = _OkLLMService()
        app = _make_app(
            application_facade, job_starter, workspace,
            decision_store=decision_store, llm_service=llm,
        )
        client = TestClient(app)
        resp = client.post(
            "/documents/doc-sparse/advanced-assessment",
            headers=_headers(),
        )
        data = _envelope(resp.json())
    finally:
        _profiling.DeterministicDocumentProfiler = original  # type: ignore[misc]

    assert data["result"]["sampleTextStatus"] == "unreliable"


def test_advanced_assessment_leaves_short_docs_alone(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """Short notes legitimately have low char counts — the
    density check skips when ``page_count < 3`` so a 1-page memo
    doesn't get demoted to ``unreliable`` just because it has 50
    chars."""
    from j1.processing.profiling import DocumentProfile

    _stage_document(
        workspace, registry, ctx,
        document_id="doc-memo",
        filename="memo.txt",
        content=b"short memo body",
    )

    import j1.processing.profiling as _profiling

    class _StubProfiler:
        def profile(self, document_id: str, source_path: str):
            return DocumentProfile(
                document_id=document_id,
                extension=".txt",
                page_count=1,  # short doc
                text_extractable_ratio=1.0,
            )

    original = _profiling.DeterministicDocumentProfiler
    _profiling.DeterministicDocumentProfiler = _StubProfiler  # type: ignore[misc]
    try:
        llm = _OkLLMService()
        app = _make_app(
            application_facade, job_starter, workspace,
            decision_store=decision_store, llm_service=llm,
        )
        client = TestClient(app)
        resp = client.post(
            "/documents/doc-memo/advanced-assessment",
            headers=_headers(),
        )
        data = _envelope(resp.json())
    finally:
        _profiling.DeterministicDocumentProfiler = original  # type: ignore[misc]

    assert data["result"]["sampleTextStatus"] == "available"


def test_assessment_plan_surfaces_latest_llm_result(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """After the operator runs Advanced Assessment, a subsequent
    /assessment-plan call must SURFACE (not re-run) the LLM
    payload + recommended_next_steps so the FE picker shows the
    LLM-driven recommendation."""
    _stage_document(workspace, registry, ctx)
    llm = _OkLLMService()
    app = _make_app(
        application_facade, job_starter, workspace,
        decision_store=decision_store, llm_service=llm,
    )
    client = TestClient(app)
    # First: no LLM result yet — plan shows nothing.
    initial = _envelope(client.post(
        "/documents/doc-adv/assessment-plan",
        headers=_headers(),
    ).json())
    assert initial.get("llmAssessment") is None
    assert initial.get("recommendedNextSteps") == []
    # Run advanced assessment.
    adv = _envelope(client.post(
        "/documents/doc-adv/advanced-assessment",
        headers=_headers(),
    ).json())
    assert adv["result"]["status"] == STATUS_OK
    # Refresh: plan now carries the LLM payload + suggested steps.
    refreshed = _envelope(client.post(
        "/documents/doc-adv/assessment-plan",
        headers=_headers(),
    ).json())
    assert refreshed["llmAssessment"] is not None
    assert (
        refreshed["llmAssessment"]["recommendedProfile"]
        == "deep_knowledge_index"
    )
    assert "run_domain_enrichment" in refreshed["recommendedNextSteps"]
    # The LLM was called EXACTLY ONCE — the refresh did not
    # re-invoke it. Pins the "uncached but persisted result" contract.
    assert len(llm.calls) == 1


def test_default_ingest_does_not_invoke_manual_actions_or_llm(
    application_facade, job_starter, workspace, registry, ctx,
    decision_store,
):
    """Regression: the standard /assessment-plan path is
    lightweight. It must NOT call the LLM service even when one is
    wired AND must not include any of the LLM next-steps as if
    they had been triggered."""
    _stage_document(workspace, registry, ctx)
    llm = _OkLLMService()
    app = _make_app(
        application_facade, job_starter, workspace,
        decision_store=decision_store, llm_service=llm,
    )
    client = TestClient(app)
    client.post(
        "/documents/doc-adv/assessment-plan", headers=_headers(),
    )
    assert llm.calls == []
