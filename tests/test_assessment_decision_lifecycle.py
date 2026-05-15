"""End-to-end tests for the assessment-decision lifecycle.

Pins the contract:

  1. ``POST /documents/{id}/assessment-plan`` mints + persists an
     ``AssessmentDecision`` and returns its id.
  2. ``POST /ingestion-runs`` consumes the same decision by id,
     validates it against the document, and stamps the full payload
     onto ``IngestionRun.metadata``.
  3. Missing / invalid decisions degrade to ``rebuilt_fallback``
     with an operator-readable warning.
  4. The metadata ``assessment_decision_source`` field tells the
     final report whether the workflow used a persisted decision.
  5. ``selectedDomainId=civil_engineering`` makes the civil pack's
     rules visible to the resolver.
  6. The dev API process preloads the registry so both packs are
     available without a worker.
  7. Hints surface in the response but never influence compile
     options directly — compile sees ``effective_profile`` only.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus
from j1.processing.assessment_decision import (
    ASSESSMENT_DECISION_SCHEMA_VERSION,
    ASSESSMENT_DECISION_SOURCE_PERSISTED,
    ASSESSMENT_DECISION_SOURCE_REBUILT_FALLBACK,
    AssessmentDecision,
    AssessmentDecisionValidationError,
    JsonlAssessmentDecisionStore,
    new_decision_id,
    validate_decision_for_document,
)
from j1.processing.execution_profile_policy import ExecutionProfilePolicy
from j1.processing.execution_profile import ExecutionProfile


_NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
TENANT_HEADER = "X-Tenant-Id"
PROJECT_HEADER = "X-Project-Id"


def _headers() -> dict[str, str]:
    return {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}


def _envelope(payload):
    assert "data" in payload, payload
    return payload["data"]


# ---- Unit: AssessmentDecision serialise / validate -----------------


def test_decision_round_trips_through_payload():
    d = AssessmentDecision(
        assessment_decision_id="ad-001",
        document_id="doc-1",
        file_hash="sha256:abc",
        selected_domain_id="general",
        lightweight_assessment={"mode": "standard"},
        matched_domain_rules=({"ruleId": "x"},),
        recommended_profile="standard",
        selected_profile=None,
        effective_profile="standard",
        recommendation_source="general_domain_rule",
        fallback_used=False,
        compile_option_preview={"suspectedTables": True},
        warnings=("ok",),
    )
    out = AssessmentDecision.from_payload(d.to_payload())
    assert out == d


def test_validation_rejects_cross_document():
    d = AssessmentDecision(
        assessment_decision_id="ad-001",
        document_id="doc-A",
        selected_domain_id="general",
        recommended_profile="standard",
        effective_profile="standard",
        recommendation_source="general_domain_rule",
        fallback_used=False,
    )
    with pytest.raises(AssessmentDecisionValidationError, match="belongs to"):
        validate_decision_for_document(d, document_id="doc-B")


def test_validation_rejects_hash_mismatch():
    d = AssessmentDecision(
        assessment_decision_id="ad-001",
        document_id="doc-A",
        file_hash="sha256:original",
        selected_domain_id="general",
        recommended_profile="standard",
        effective_profile="standard",
        recommendation_source="general_domain_rule",
        fallback_used=False,
    )
    with pytest.raises(AssessmentDecisionValidationError, match="different file"):
        validate_decision_for_document(
            d, document_id="doc-A", file_hash="sha256:changed",
        )


def test_validation_accepts_when_hash_absent_on_either_side():
    """Legacy paths where the doc has no hash, or the decision was
    built before we recorded one — both succeed (no false rejection)."""
    d = AssessmentDecision(
        assessment_decision_id="ad-001",
        document_id="doc-A",
        file_hash=None,  # no hash stamped
        selected_domain_id="general",
        recommended_profile="standard",
        effective_profile="standard",
        recommendation_source="general_domain_rule",
        fallback_used=False,
    )
    # Either side missing → no rejection.
    validate_decision_for_document(d, document_id="doc-A", file_hash="sha256:x")
    validate_decision_for_document(d, document_id="doc-A", file_hash=None)


def test_validation_rejects_unsupported_schema():
    d = AssessmentDecision(
        assessment_decision_id="ad-001",
        document_id="doc-A",
        selected_domain_id="general",
        recommended_profile="standard",
        effective_profile="standard",
        recommendation_source="general_domain_rule",
        fallback_used=False,
        schema_version="99",
    )
    with pytest.raises(AssessmentDecisionValidationError, match="schema"):
        validate_decision_for_document(d, document_id="doc-A")


# ---- JsonlAssessmentDecisionStore round-trip ----------------------


def test_jsonl_store_round_trips_decisions(workspace, ctx):
    store = JsonlAssessmentDecisionStore(workspace)
    decision = AssessmentDecision(
        assessment_decision_id="ad-store-1",
        document_id="doc-1",
        selected_domain_id="civil_engineering",
        lightweight_assessment={"mode": "deep"},
        matched_domain_rules=({"ruleId": "civil_rfp_tender"},),
        recommended_profile="advanced",
        effective_profile="advanced",
        recommendation_source="active_domain_rule",
        fallback_used=False,
        warnings=("note 1", "note 2"),
    )
    store.upsert(ctx, decision)
    fetched = store.get(ctx, "ad-store-1")
    assert fetched is not None
    assert fetched.assessment_decision_id == "ad-store-1"
    assert fetched.recommended_profile == "advanced"
    assert fetched.matched_domain_rules == ({"ruleId": "civil_rfp_tender"},)
    # Unknown id → None, not exception.
    assert store.get(ctx, "ad-missing") is None


# ---- Domain registry preload -------------------------------------


def test_default_registry_includes_civil_engineering_in_standalone():
    """Regression for the bug where a standalone API server (no
    worker in-process) would never import the civil pack. The dev
    API startup calls ``default_registry()`` to force the import."""
    from j1.domains import default_registry
    registry = default_registry()
    assert registry.get("civil_engineering") is not None
    civil = registry.get("civil_engineering")
    rule_ids = {r.id for r in civil.document_profile_rules}
    assert "civil_rfp_tender" in rule_ids


# ---- Linter --------------------------------------------------------


def test_linter_rejects_catchall_regex():
    from j1.domains.profile_rules import (
        DocumentProfileRuleLintError,
        lint_document_profile_rule,
    )
    with pytest.raises(DocumentProfileRuleLintError, match="catch-all"):
        lint_document_profile_rule(
            "bad",
            priority=10,
            recommended_profile="advanced",
            reason="matches everything",
            filename_regex=".*",
            title_regex=None,
        )


def test_linter_accepts_catchall_when_explicit():
    from j1.domains.profile_rules import lint_document_profile_rule
    lint_document_profile_rule(
        "intentional",
        priority=10,
        recommended_profile="standard",
        reason="explicit catch-all for staging deployments",
        filename_regex=".*",
        title_regex=None,
        allow_catchall=True,
    )  # no raise


def test_linter_rejects_uncompilable_regex():
    from j1.domains.profile_rules import (
        DocumentProfileRuleLintError,
        lint_document_profile_rule,
    )
    with pytest.raises(DocumentProfileRuleLintError, match="does not compile"):
        lint_document_profile_rule(
            "broken",
            priority=10,
            recommended_profile="advanced",
            reason="malformed pattern",
            filename_regex="(unbalanced[",
            title_regex=None,
        )


def test_linter_requires_reason():
    from j1.domains.profile_rules import (
        DocumentProfileRuleLintError,
        lint_document_profile_rule,
    )
    with pytest.raises(DocumentProfileRuleLintError, match="reason"):
        lint_document_profile_rule(
            "no_reason",
            priority=10,
            recommended_profile="advanced",
            reason="",
            filename_regex=r"\bfoo\b",
            title_regex=None,
        )


def test_linter_rejects_unknown_profile():
    from j1.domains.profile_rules import (
        DocumentProfileRuleLintError,
        lint_document_profile_rule,
    )
    with pytest.raises(DocumentProfileRuleLintError, match="recommended_profile"):
        lint_document_profile_rule(
            "bad",
            priority=10,
            recommended_profile="ultra_pro_max",
            reason="bogus",
            filename_regex=r"\bfoo\b",
            title_regex=None,
        )


def test_linter_treats_case_insensitive_catchall_correctly():
    """The catch-all guard must strip leading flag groups like
    ``(?i)`` so case-insensitive variants of ``.*`` are still
    caught."""
    from j1.domains.profile_rules import (
        DocumentProfileRuleLintError,
        lint_document_profile_rule,
    )
    with pytest.raises(DocumentProfileRuleLintError, match="catch-all"):
        lint_document_profile_rule(
            "sneaky",
            priority=10,
            recommended_profile="advanced",
            reason="r",
            filename_regex="(?i).*",
            title_regex=None,
        )


# ---- REST: full decision lifecycle --------------------------------


@pytest.fixture
def decision_store(workspace):
    return JsonlAssessmentDecisionStore(workspace)


@pytest.fixture
def ingestion_run_store(workspace):
    from j1.runs.store import JsonlIngestionRunStore
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, audit_recorder,
):
    """Minimal facade — assessment-plan only needs source_lookup +
    ingestion; the larger fixture in ``test_rest_adapter.py`` carries
    a lot of unrelated wiring."""
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
        return f"job-{document_id}-{len(started_jobs)}"
    starter.bodies = []
    return starter


@pytest.fixture
def app_with_decisions(
    application_facade, job_starter, workspace, decision_store,
    ingestion_run_store,
):
    from j1.integration.dto import ProcessingCapabilities
    caps = ProcessingCapabilities(
        compiler_kinds=frozenset({"mineru"}),
        default_compiler_kind="mineru",
        enricher_kinds=frozenset(),
        graph_builder_kinds=frozenset(),
        indexer_kinds=frozenset(),
    )
    return create_rest_api(
        application_facade,
        job_starter=job_starter,
        workspace=workspace,
        ingestion_run_store=ingestion_run_store,
        assessment_decision_store=decision_store,
        processing_capabilities=caps,
        execution_profile_policy=ExecutionProfilePolicy(
            default_profile=ExecutionProfile.STANDARD,
            allowed=frozenset(ExecutionProfile),
        ),
    )


@pytest.fixture
def decision_client(app_with_decisions):
    return TestClient(app_with_decisions)


def _register_doc_with_file(
    workspace, registry, ctx, *,
    document_id: str = "doc-rfp",
    filename: str = "ProjectX_RFP_2026.pdf",
    content: bytes = b"hello rfp content",
):
    """Stage a registered document AND write its source file under the
    workspace's raw area. The checksum is the real sha256 of the
    content so a follow-up upload via ``/ingestion-runs`` of the same
    bytes resolves via ``DuplicateDocumentError`` back to this same
    ``document_id`` (the contract that makes the assessment-decision
    plumbing work end-to-end)."""
    import hashlib
    raw_dir = workspace.raw(ctx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / filename).write_bytes(content)
    real_checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=filename,
        stored_filename=filename,
        mime_type="application/pdf",
        file_size=len(content),
        checksum=real_checksum,
        status=ProcessingStatus.PENDING,
        created_at=_NOW,
    )
    registry.add(record)
    return record


def test_assessment_plan_returns_decision_id_and_persists_it(
    decision_client, ctx, registry, workspace, decision_store,
):
    """REST contract: every assessment-plan response that has a
    store wired returns ``assessmentDecisionId`` AND persists the
    same record under (ctx, decision_id) lookup."""
    _register_doc_with_file(workspace, registry, ctx)
    resp = decision_client.post(
        "/documents/doc-rfp/assessment-plan",
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = _envelope(resp.json())
    decision_id = data["assessmentDecisionId"]
    assert decision_id is not None
    assert decision_id.startswith("ad-")
    # Persisted under the decision-store lookup.
    persisted = decision_store.get(ctx, decision_id)
    assert persisted is not None
    assert persisted.document_id == "doc-rfp"
    assert persisted.recommended_profile == data["recommendedProfile"]
    assert persisted.recommendation_source == data["recommendationSource"]
    assert persisted.selected_domain_id == data["selectedDomainId"]


def test_assessment_plan_accepts_selected_domain_id(
    decision_client, ctx, registry, workspace, decision_store,
):
    """User-supplied ``selectedDomainId=civil_engineering`` makes the
    civil pack's rules visible; the resulting recommendation cites
    the civil RFP rule, not the generic one."""
    _register_doc_with_file(
        workspace, registry, ctx,
        document_id="doc-civil-rfp",
        filename="ProjectX_RFP_2026.pdf",
    )
    resp = decision_client.post(
        "/documents/doc-civil-rfp/assessment-plan",
        headers=_headers(),
        json={"selectedDomainId": "civil_engineering"},
    )
    data = _envelope(resp.json())
    assert data["selectedDomainId"] == "civil_engineering"
    winner = next(
        (r for r in data["matchedRules"] if r["winner"]), None,
    )
    assert winner is not None
    assert winner["domainId"] == "civil_engineering"
    assert winner["ruleId"] == "civil_rfp_tender"
    assert data["recommendedProfile"] == "advanced"


def test_assessment_plan_falls_back_to_general_for_unknown_domain(
    decision_client, ctx, registry, workspace, decision_store,
):
    """An unknown ``selectedDomainId`` does NOT 4xx — it falls back
    to general and surfaces a warning so the operator sees the
    mismatch."""
    _register_doc_with_file(workspace, registry, ctx)
    resp = decision_client.post(
        "/documents/doc-rfp/assessment-plan",
        headers=_headers(),
        json={"selectedDomainId": "nonexistent_pack"},
    )
    assert resp.status_code == 200
    data = _envelope(resp.json())
    assert data["selectedDomainId"] == "general"
    assert any(
        "nonexistent_pack" in w and "general" in w
        for w in data["warnings"]
    )


def test_ingestion_run_consumes_assessment_decision(
    decision_client, ctx, registry, workspace, decision_store,
    ingestion_run_store,
):
    """REST flow: ``/assessment-plan`` → ``/ingestion-runs`` with
    ``assessmentDecisionId``. The run's metadata stamps the full
    decision payload and ``assessment_decision_source="persisted"``."""
    upload_content = b"hello rfp content"
    _register_doc_with_file(
        workspace, registry, ctx,
        document_id="doc-flow",
        filename="ProjectX_RFP_2026.txt",
        content=upload_content,
    )
    # 1. Assess → mint decision id.
    plan_resp = decision_client.post(
        "/documents/doc-flow/assessment-plan",
        headers=_headers(),
        json={"selectedDomainId": "civil_engineering"},
    )
    plan = _envelope(plan_resp.json())
    decision_id = plan["assessmentDecisionId"]
    assert decision_id is not None

    # 2. Upload + start with the same decision id. Uploading the
    # SAME bytes hits the intake service's duplicate-checksum path,
    # which resolves to the seeded ``doc-flow`` id so the decision
    # validation succeeds. Real flows hit this same idempotency
    # contract when the FE uploads → assesses → re-uploads to start.
    upload_resp = decision_client.post(
        "/ingestion-runs",
        files={"file": ("ProjectX_RFP_2026.txt", io.BytesIO(upload_content), "text/plain")},
        data={
            "selectedProfile": plan["recommendedProfile"],
            "assessmentDecisionId": decision_id,
        },
        headers=_headers(),
    )
    assert upload_resp.status_code == 201, upload_resp.text
    run_id = _envelope(upload_resp.json())["runId"]

    # 3. Run record carries the decision + source = persisted.
    run = ingestion_run_store.get(ctx, run_id)
    assert run is not None
    assert run.metadata.get("assessment_decision_id") == decision_id
    assert run.metadata.get("assessment_decision_source") == (
        ASSESSMENT_DECISION_SOURCE_PERSISTED
    )
    stamped_decision = run.metadata.get("assessment_decision")
    assert stamped_decision is not None
    assert stamped_decision["assessmentDecisionId"] == decision_id
    assert (
        stamped_decision["recommendedProfile"] == plan["recommendedProfile"]
    )


def test_ingestion_run_with_unknown_decision_id_records_fallback(
    decision_client, ctx, registry, workspace, ingestion_run_store,
):
    """A bogus decision id never 4xxs (assessment is advisory). The
    run still starts, but ``assessment_decision_source`` switches to
    ``rebuilt_fallback`` and the validation warning is stamped onto
    metadata so the final report reflects what happened."""
    upload_resp = decision_client.post(
        "/ingestion-runs",
        files={"file": ("doc.txt", io.BytesIO(b"hello"), "text/plain")},
        data={
            "selectedProfile": "standard",
            "assessmentDecisionId": "ad-never-existed",
        },
        headers=_headers(),
    )
    assert upload_resp.status_code == 201
    run_id = _envelope(upload_resp.json())["runId"]
    run = ingestion_run_store.get(ctx, run_id)
    assert run.metadata.get("assessment_decision_source") == (
        ASSESSMENT_DECISION_SOURCE_REBUILT_FALLBACK
    )
    warnings = run.metadata.get("assessment_decision_warnings") or []
    assert any("not found" in w for w in warnings)


def test_ingestion_run_with_mismatched_decision_records_fallback(
    decision_client, ctx, registry, workspace, decision_store,
    ingestion_run_store,
):
    """A decision that belongs to a DIFFERENT document must not be
    accepted. We stamp ``rebuilt_fallback`` + warning instead of
    silently using the wrong recommendation."""
    # Pre-seed a decision for doc-OTHER.
    other_decision = AssessmentDecision(
        assessment_decision_id="ad-other",
        document_id="doc-OTHER",
        selected_domain_id="general",
        lightweight_assessment={"mode": "standard"},
        recommended_profile="advanced",
        effective_profile="advanced",
        recommendation_source="general_domain_rule",
        fallback_used=False,
    )
    decision_store.upsert(ctx, other_decision)

    # Try to ingest a DIFFERENT document with that decision id.
    upload_resp = decision_client.post(
        "/ingestion-runs",
        files={"file": ("mine.txt", io.BytesIO(b"hello"), "text/plain")},
        data={
            "selectedProfile": "standard",
            "assessmentDecisionId": "ad-other",
        },
        headers=_headers(),
    )
    assert upload_resp.status_code == 201
    run_id = _envelope(upload_resp.json())["runId"]
    run = ingestion_run_store.get(ctx, run_id)
    assert run.metadata.get("assessment_decision_source") == (
        ASSESSMENT_DECISION_SOURCE_REBUILT_FALLBACK
    )
    warnings = run.metadata.get("assessment_decision_warnings") or []
    assert any("belongs to document" in w for w in warnings)


# ---- IngestRequest threading --------------------------------------


def test_ingest_request_carries_decision_payload_to_workflow():
    """Regression: the REST adapter populates BOTH
    ``assessment_decision_id`` AND ``assessment_decision_payload`` on
    the IngestRequest so the workflow can short-circuit without
    calling the store from inside Temporal."""
    from j1.adapters.rest.schemas import IngestRequest
    payload = {"assessmentDecisionId": "ad-1", "lightweightAssessment": {"mode": "deep"}}
    req = IngestRequest(
        actor="system",
        selected_profile="standard",
        assessment_decision_id="ad-1",
        assessment_decision_payload=payload,
        assessment_decision_warnings=("note",),
    )
    assert req.assessment_decision_id == "ad-1"
    assert req.assessment_decision_payload == payload
    assert req.assessment_decision_warnings == ("note",)


# ---- ProjectProcessingRequest threading --------------------------


def test_workflow_request_carries_decision_payload():
    """The decision payload reaches the workflow via
    ``ProjectProcessingRequest`` so the workflow can read it and
    decide whether to skip the build-initial-execution-plan
    activity."""
    from j1.orchestration.workflows.project_processing import (
        ProjectProcessingRequest,
    )
    from j1.orchestration.activities.payloads import ProjectScope
    scope = ProjectScope(tenant_id="acme", project_id="alpha")
    req = ProjectProcessingRequest(
        scope=scope,
        compiler_kind="mineru",
        assessment_decision_payload={
            "assessmentDecisionId": "ad-1",
            "lightweightAssessment": {"mode": "deep"},
        },
    )
    assert (
        req.assessment_decision_payload["assessmentDecisionId"] == "ad-1"
    )


# ---- Hints stay advisory ------------------------------------------


def test_assessment_response_surfaces_hints_in_preview_only(
    decision_client, ctx, registry, workspace,
):
    """The ``compileOptionPreview`` block surfaces hints to the FE as
    ``suspected*`` flags. The endpoint MUST NOT return compile-stage
    boolean flags that would override the workflow's authority over
    actual compile behaviour."""
    _register_doc_with_file(
        workspace, registry, ctx,
        document_id="doc-civil-rfp",
        filename="ProjectX_RFP_2026.pdf",
    )
    resp = decision_client.post(
        "/documents/doc-civil-rfp/assessment-plan",
        headers=_headers(),
        json={"selectedDomainId": "civil_engineering"},
    )
    data = _envelope(resp.json())
    preview = data["compileOptionPreview"]
    # The hedged shape — never plain booleans like "enableTables"
    # that would imply compile-stage authority.
    assert "suspectedTables" in preview
    assert "suspectedRequirements" in preview
    assert preview["suspectedTables"] is True
    assert preview["suspectedRequirements"] is True
    # No keys that would imply direct compile-option control.
    for forbidden in (
        "enable_ocr", "enableOcr", "force_tables", "forceTables",
        "use_vision", "useVision",
    ):
        assert forbidden not in preview
    # The note disclaims authority explicitly.
    assert "rule-based hints" in preview["note"].lower()
    assert "not exact detection" in preview["note"].lower()
