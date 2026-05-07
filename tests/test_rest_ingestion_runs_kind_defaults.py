"""Regression tests for the FE-upload kind-defaulting fix.

The bug: `POST /ingestion-runs` (the user-facing FE upload path)
omitted enricher / graph_builder / indexer kinds. The previous
`_validate_optional_processor_kind` returned None on every omission,
which collapsed the workflow's `available_steps` to `{compile}` and
silently skipped every other stage — even when the deployment had
those processors wired.

The fix: `_resolve_optional_processor_kind` auto-picks when the
deployment has exactly one registered kind for the role. Multiple
registered → operator must choose explicitly; none registered →
stage stays unrunnable (preserves backward compatibility).

These tests pin the three branches of the new helper end-to-end via
the upload endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.integration.dto import ProcessingCapabilities
from j1.projects.context import ProjectContext
from j1.runs import AuditProgressReporter, JsonlIngestionRunStore


_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def reporter(workspace):
    return AuditProgressReporter(DefaultAuditRecorder(JsonlAuditSink(workspace)))


@pytest.fixture
def feedback_store(workspace):
    from j1.integration import JsonlFeedbackStore
    return JsonlFeedbackStore(workspace.audit(ProjectContext(
        tenant_id="acme", project_id="alpha",
    )) / "feedback.jsonl")


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, feedback_store, audit_recorder,
):
    from j1.integration import (
        ApplicationFacade, CitationLookupService,
        DocumentIngestionService, EventPublisherService,
        FeedbackService, RetrievalService, SourceLookupService,
    )
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
    )


def _make_client(application_facade, workspace, run_store, reporter,
                 capabilities: ProcessingCapabilities, started: list):
    """Build a TestClient whose `job_starter` captures the full
    body object so each test can assert the kinds the workflow saw."""
    async def starter(_ctx, document_id, body):
        started.append({
            "document_id": document_id,
            "compiler_kind": body.compiler_kind,
            "enricher_kind": body.enricher_kind,
            "graph_builder_kind": body.graph_builder_kind,
            "indexer_kind": body.indexer_kind,
        })
        return f"wf-{document_id}"
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        progress_reporter=reporter,
        job_starter=starter,
        processing_capabilities=capabilities,
    )
    return TestClient(app)


def _post_upload(client: TestClient) -> dict:
    files = {"file": ("hello.txt", b"hello world", "text/plain")}
    resp = client.post(
        "/ingestion-runs",
        files=files,
        # NB: deliberately NO compilerKind / enricherKind / graphBuilderKind
        # / indexerKind in the form data — same shape as the FE upload.
        headers=_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


# ---- Auto-default branch: exactly one registered ------------------


def test_omitted_indexer_kind_auto_defaults_when_exactly_one_registered(
    application_facade, workspace, run_store, reporter,
):
    """The dev stack registers exactly one indexer (`SqliteSearchIndexer`).
    The FE upload omits `indexerKind`; the workflow MUST receive
    `indexer_kind="sqlite_indexer"` so `STEP_INDEX` enters
    `available_steps` and the planner doesn't drop the index stage."""
    started: list[dict] = []
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
        graph_builder_kinds=frozenset(),
        enricher_kinds=frozenset(),
        indexer_kinds=frozenset({"sqlite_indexer"}),
    )
    client = _make_client(
        application_facade, workspace, run_store, reporter,
        capabilities, started,
    )

    _post_upload(client)

    assert len(started) == 1
    # Auto-default kicked in for indexer.
    assert started[0]["indexer_kind"] == "sqlite_indexer"
    # No graph or enricher registered → both stay None.
    assert started[0]["graph_builder_kind"] is None
    assert started[0]["enricher_kind"] is None


def test_omitted_graph_kind_auto_defaults_for_unique_registered(
    application_facade, workspace, run_store, reporter,
):
    """The default dev `J1_DEFAULT_GRAPH_PROVIDER=raganything` stack
    registers exactly one graph builder; the FE upload should pick
    it up automatically so the Graph stage runs."""
    started: list[dict] = []
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
        graph_builder_kinds=frozenset({"raganything"}),
        enricher_kinds=frozenset(),
        indexer_kinds=frozenset({"sqlite_indexer"}),
    )
    client = _make_client(
        application_facade, workspace, run_store, reporter,
        capabilities, started,
    )

    _post_upload(client)

    assert started[0]["graph_builder_kind"] == "raganything"
    assert started[0]["indexer_kind"] == "sqlite_indexer"


# ---- Multiple-registered branch: leave None ----------------------


def test_omitted_kind_stays_none_when_multiple_registered(
    application_facade, workspace, run_store, reporter,
):
    """Two graph builders are wired (e.g. `raganything` + `graphify`).
    Picking one without operator intent would surprise the deployment;
    leave None so the planner skips the stage with `source=CALLER` and
    the operator chooses explicitly."""
    started: list[dict] = []
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
        graph_builder_kinds=frozenset({"raganything", "graphify"}),
        enricher_kinds=frozenset(),
        indexer_kinds=frozenset({"sqlite_indexer"}),
    )
    client = _make_client(
        application_facade, workspace, run_store, reporter,
        capabilities, started,
    )

    _post_upload(client)

    assert started[0]["graph_builder_kind"] is None
    # Indexer stays auto-defaulted (only one registered).
    assert started[0]["indexer_kind"] == "sqlite_indexer"


# ---- None-registered branch: leave None --------------------------


def test_omitted_kind_stays_none_when_none_registered(
    application_facade, workspace, run_store, reporter,
):
    """No enricher wired → enricher_kind stays None → enrich stage
    is skipped at the workflow boundary with a clear reason. This
    matches today's dev-stack reality (worker.py passes no
    enrichers)."""
    started: list[dict] = []
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
        graph_builder_kinds=frozenset(),
        enricher_kinds=frozenset(),
        indexer_kinds=frozenset(),
    )
    client = _make_client(
        application_facade, workspace, run_store, reporter,
        capabilities, started,
    )

    _post_upload(client)

    assert started[0]["enricher_kind"] is None
    assert started[0]["graph_builder_kind"] is None
    assert started[0]["indexer_kind"] is None


# ---- Caller-supplied still validated ----------------------------


def test_caller_supplied_kind_still_validated_against_registered(
    application_facade, workspace, run_store, reporter,
):
    """The auto-default semantic must NOT relax the caller-supplied
    validation path. A typo'd `graphBuilderKind` against a known
    set should still 400 at the boundary."""
    started: list[dict] = []
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
        graph_builder_kinds=frozenset({"raganything"}),
        enricher_kinds=frozenset(),
        indexer_kinds=frozenset({"sqlite_indexer"}),
    )
    client = _make_client(
        application_facade, workspace, run_store, reporter,
        capabilities, started,
    )

    files = {"file": ("hello.txt", b"x", "text/plain")}
    resp = client.post(
        "/ingestion-runs",
        files=files,
        data={"graphBuilderKind": "not_registered_kind"},
        headers=_HEADERS,
    )
    assert resp.status_code == 400
    assert "unknown graphBuilderKind" in resp.json()["error"]["message"]


def test_caller_supplied_kind_passes_through_unchanged(
    application_facade, workspace, run_store, reporter,
):
    """When the caller DID pin a kind, the helper passes it through
    even if a different default would be auto-picked from a single-
    registered set (caller wins)."""
    started: list[dict] = []
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
        graph_builder_kinds=frozenset({"raganything", "graphify"}),
        enricher_kinds=frozenset(),
        indexer_kinds=frozenset({"sqlite_indexer"}),
    )
    client = _make_client(
        application_facade, workspace, run_store, reporter,
        capabilities, started,
    )

    files = {"file": ("hello.txt", b"x", "text/plain")}
    resp = client.post(
        "/ingestion-runs",
        files=files,
        data={"graphBuilderKind": "graphify"},
        headers=_HEADERS,
    )
    assert resp.status_code == 201
    assert started[0]["graph_builder_kind"] == "graphify"
