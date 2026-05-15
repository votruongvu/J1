"""REST tests for the first wired post-index Manual Action:
``POST /documents/{document_id}/manual-actions/run-domain-enrichment``.

Pins the contracts the FE relies on:

  * 404 on unknown document.
  * 409 on detached / removed documents.
  * 409 when the document has no active snapshot (no successful
    baseline run yet).
  * 409 when another run is already in flight against the document.
  * 403 when the deployment turned the feature off.
  * Happy path: creates a candidate run with
    ``run_type="run_domain_enrichment"``,
    ``metadata.reused_compile_from_run_id=<source>``, and
    ``metadata.manual_action="run_domain_enrichment"``.
  * Idempotent under repeated rapid clicks (second concurrent
    POST returns 409, not a parallel duplicate run).
  * RAGAnything compile is NOT re-invoked — the workflow reuses
    compile via the metadata hint.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.jobs.status import ProcessingStatus
from j1.processing.manual_actions import (
    ENV_MANUAL_ACTIONS,
    ENV_MANUAL_DOMAIN_ENRICHMENT,
)
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)


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
        job_control=None,
    )


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def started_jobs() -> list:
    return []


@pytest.fixture
def job_starter(started_jobs):
    async def starter(ctx, document_id, body):
        started_jobs.append({
            "document_id": document_id,
            "correlation_id": body.correlation_id,
            "reindex_of": body.reindex_of,
            "target_snapshot_id": body.target_snapshot_id,
        })
        return f"wf-{body.correlation_id}"
    return starter


@pytest.fixture
def lifecycle_service(workspace, registry, artifact_registry):
    return DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        clock=lambda: _NOW,
    )


@pytest.fixture
def client(
    application_facade, workspace, run_store, job_starter,
    lifecycle_service,
):
    from j1.integration.dto import ProcessingCapabilities
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
    )
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        job_starter=job_starter,
        document_lifecycle_service=lifecycle_service,
        processing_capabilities=capabilities,
    )
    return TestClient(app)


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


def _seed_doc(
    registry, ctx, *, document_id="doc-1", state="attached",
    active_snapshot_id="snap-active",
):
    registry.add(DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state=state,  # type: ignore[arg-type]
        active_snapshot_id=active_snapshot_id,
    ))


def _seed_run(
    run_store, ctx, *, run_id, document_id,
    status=RunStatus.SUCCEEDED, metadata=None,
    started_at: datetime | None = None,
):
    started = started_at or _NOW
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=started,
        updated_at=started,
        completed_at=started,
        metadata=metadata or {},
    ))


_PATH = "/documents/{doc}/manual-actions/run-domain-enrichment"


# ---- Happy path -----------------------------------------------------


def test_endpoint_creates_run_domain_enrichment_candidate(
    client, registry, run_store, started_jobs, ctx,
):
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")

    resp = client.post(
        _PATH.format(doc="doc-1"), headers=_headers(ctx),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]

    assert body["documentId"] == "doc-1"
    assert body["manualAction"] == "run_domain_enrichment"
    assert body["runType"] == "run_domain_enrichment"
    assert body["parentRunId"] == "r-baseline"
    assert body["sourceRunId"] == "r-baseline"
    assert body["sourceSnapshotId"] == "snap-active"
    assert body["status"] == "queued"
    new_run_id = body["manualActionRunId"]

    new_run = run_store.get(ctx, new_run_id)
    assert new_run is not None
    assert new_run.run_type == "run_domain_enrichment"
    assert new_run.parent_run_id == "r-baseline"
    assert new_run.metadata["reused_compile_from_run_id"] == "r-baseline"
    assert new_run.metadata["manual_action"] == "run_domain_enrichment"
    assert new_run.metadata["manual_action_source_snapshot_id"] == "snap-active"

    # The job starter saw a dispatch with reindex_of=parent so the
    # compile activity reuses the baseline run's artifacts (no
    # MinerU / RAGAnything re-parse).
    assert len(started_jobs) == 1
    assert started_jobs[0]["document_id"] == "doc-1"
    assert started_jobs[0]["reindex_of"] == "r-baseline"
    assert started_jobs[0]["correlation_id"] == new_run_id


def test_endpoint_does_not_flip_active_snapshot_immediately(
    client, registry, run_store, ctx,
):
    """Promotion is CAS-on-terminal-success — the POST only ALLOCATES
    a candidate. The current active stays live until the workflow
    succeeds."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")

    client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert registry.get(ctx, "doc-1").active_snapshot_id == "snap-active"


# ---- Refusal paths --------------------------------------------------


def test_endpoint_404_on_unknown_document(client, ctx):
    resp = client.post(_PATH.format(doc="ghost"), headers=_headers(ctx))
    assert resp.status_code == 404


def test_endpoint_409_when_document_detached(client, registry, run_store, ctx):
    _seed_doc(
        registry, ctx, state="detached", active_snapshot_id="snap-active",
    )
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")
    resp = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert resp.status_code == 409
    assert "detached" in resp.text.lower()


def test_endpoint_409_when_document_removed(client, registry, run_store, ctx):
    _seed_doc(
        registry, ctx, state="removed", active_snapshot_id=None,
    )
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")
    resp = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert resp.status_code == 409
    assert "removed" in resp.text.lower()


def test_endpoint_409_when_no_active_snapshot(
    client, registry, run_store, ctx,
):
    """Without an active snapshot there is no baseline compile to
    reuse — the spec requires the user to run an initial ingest
    before triggering manual actions."""
    _seed_doc(registry, ctx, active_snapshot_id=None)
    resp = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert resp.status_code == 409
    assert "no active snapshot" in resp.text.lower()


def test_endpoint_409_when_baseline_run_missing(
    client, registry, run_store, ctx,
):
    """Active snapshot pointer set but no SUCCEEDED run on the
    document — refuse rather than silently dispatch a manual action
    that has nothing to reuse."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    # Only a failed run is present.
    _seed_run(
        run_store, ctx, run_id="r-failed",
        document_id="doc-1", status=RunStatus.FAILED,
    )
    resp = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert resp.status_code == 409


def test_endpoint_409_when_another_run_in_flight(
    client, registry, run_store, ctx,
):
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")
    _seed_run(
        run_store, ctx, run_id="r-inflight",
        document_id="doc-1", status=RunStatus.RUNNING,
        started_at=_NOW + timedelta(seconds=1),
    )
    resp = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert resp.status_code == 409
    assert "in flight" in resp.text.lower() or "currently" in resp.text.lower()


def test_endpoint_blocks_duplicate_concurrent_action(
    client, registry, run_store, ctx,
):
    """Two rapid POSTs from a double-click must not race into two
    parallel manual-action runs. The second one sees the first as
    in-flight and 409s."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")

    first = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert first.status_code == 200, first.text

    second = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert second.status_code == 409, second.text


# ---- Feature-flag gate ----------------------------------------------


def test_endpoint_403_when_feature_flag_disabled(
    client, registry, run_store, ctx, monkeypatch,
):
    monkeypatch.setenv(ENV_MANUAL_ACTIONS, "false")
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")

    resp = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert resp.status_code == 403


def test_endpoint_403_when_per_action_flag_disabled(
    client, registry, run_store, ctx, monkeypatch,
):
    """Per-action override: the deployment can leave the panel
    enabled but turn off the specific cost-bearing action."""
    monkeypatch.setenv(ENV_MANUAL_ACTIONS, "true")
    monkeypatch.setenv(ENV_MANUAL_DOMAIN_ENRICHMENT, "false")
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")

    resp = client.post(_PATH.format(doc="doc-1"), headers=_headers(ctx))
    assert resp.status_code == 403


def test_manual_actions_list_reports_disabled_when_flag_off(
    client, registry, ctx, monkeypatch,
):
    """``GET /documents/{id}/manual-actions`` reflects the env flag —
    so the FE can render a clear "disabled by deployment" pill
    instead of a generic broken state."""
    monkeypatch.setenv(ENV_MANUAL_DOMAIN_ENRICHMENT, "false")
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")

    resp = client.get(
        "/documents/doc-1/manual-actions", headers=_headers(ctx),
    )
    assert resp.status_code == 200
    actions = resp.json()["data"]["actions"]
    by_id = {a["id"]: a for a in actions}
    assert by_id["run_domain_enrichment"]["status"] == "disabled"


# ---- Dispatch-failure / best-effort lock release -------------------


def test_dispatch_failure_logs_release_failure_with_structured_context(
    application_facade, workspace, run_store, registry,
    lifecycle_service, ctx, caplog,
):
    """When workflow dispatch fails AND the best-effort lock
    release ALSO fails, the handler must log a structured error
    with enough context to manually diagnose the stuck operation
    (instead of silently swallowing it). The request itself still
    raises so the caller sees the dispatch failure."""

    import logging
    from j1.integration.dto import ProcessingCapabilities
    from j1.adapters.rest import create_rest_api

    async def boom_starter(ctx, document_id, body):
        raise RuntimeError("workflow dispatch unavailable")

    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
    )

    # Wrap the source registry so its ``release_operation_lock``
    # explodes. The handler should catch the explosion and log it
    # instead of letting it propagate.
    real_sources = registry

    class _ExplodingSources:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "release_operation_lock":
                def _release(*_a, **_kw):
                    raise RuntimeError("lock store unavailable")
                return _release
            return getattr(self._inner, name)

    from j1.integration import SourceLookupService

    facade = application_facade.__class__(
        ingestion=application_facade.ingestion,
        retrieval=application_facade.retrieval,
        citation_lookup=application_facade.citation_lookup,
        source_lookup=SourceLookupService(_ExplodingSources(real_sources)),
        feedback=None,
        event_publisher=application_facade.event_publisher,
        job_control=None,
    )

    app = create_rest_api(
        facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        job_starter=boom_starter,
        document_lifecycle_service=lifecycle_service,
        processing_capabilities=capabilities,
    )
    test_client = TestClient(app, raise_server_exceptions=False)

    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-baseline", document_id="doc-1")

    with caplog.at_level(logging.ERROR, logger="j1.adapters.rest"):
        # The endpoint re-raises after logging — TestClient with
        # raise_server_exceptions=False surfaces it as HTTP 500.
        resp = test_client.post(
            _PATH.format(doc="doc-1"), headers=_headers(ctx),
        )
        assert resp.status_code == 500

    matching = [
        r for r in caplog.records
        if r.name == "j1.adapters.rest"
        and "release pending operation" in r.getMessage().lower()
    ]
    assert matching, (
        "expected an error log about failed lock release; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    rec = matching[0]
    # Structured fields used by operators to diagnose stuck locks.
    assert getattr(rec, "document_id", None) == "doc-1"
    assert getattr(rec, "operation_type", None) == "run_domain_enrichment"
    assert (
        getattr(rec, "action_type", None)
        == "manual_action.run_domain_enrichment"
    )
    # Both error reprs are captured so an operator can correlate
    # the dispatch failure with the release failure.
    assert "workflow dispatch unavailable" in getattr(
        rec, "dispatch_error", "",
    )
    assert "lock store unavailable" in getattr(rec, "release_error", "")
