"""REST tests for `POST /documents/{id}/repair`.

The endpoint exposes the orphan-invalidation sweep on demand so
operators can clean stale `run_id=None` artifacts without
dispatching a full reindex. Idempotent: a second call invalidates
zero (everything already invalid).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.artifacts.models import ArtifactRecord
from j1.documents.artifact_state import SEARCH_STATE_INVALID
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


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
def client(application_facade, workspace):
    app = create_rest_api(application_facade, workspace=workspace)
    return TestClient(app)


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


def _seed_orphan_artifact(
    artifact_registry, ctx: ProjectContext,
    *, artifact_id: str, document_id: str,
) -> None:
    """Stage an artifact with NO `run_id` in metadata — the exact
 orphan the repair sweep targets."""
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="graph_json",
        location=f"graph/{artifact_id}.json",
        content_hash=f"sha256:{artifact_id}",
        byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=[document_id],
        source_artifact_ids=[],
        metadata={},  # no run_id — this is the bug pattern
    ))


# ---- Happy path -----------------------------------------------


def test_repair_invalidates_orphans_for_target_document(
    client, artifact_registry, ctx,
):
    """Headline: orphan artifacts (run_id=None) tied to this
 document get `search_state=invalid` so retrieval drops them."""
    _seed_orphan_artifact(
        artifact_registry, ctx,
        artifact_id="orphan-1", document_id="doc-1",
    )
    _seed_orphan_artifact(
        artifact_registry, ctx,
        artifact_id="orphan-2", document_id="doc-1",
    )

    resp = client.post("/documents/doc-1/repair", headers=_headers(ctx))

    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["documentId"] == "doc-1"
    assert body["invalidatedArtifactCount"] == 2
    # Artifacts now stamped invalid (retrieval will skip them).
    a1 = artifact_registry.get(ctx, "orphan-1")
    a2 = artifact_registry.get(ctx, "orphan-2")
    assert a1.metadata["search_state"] == SEARCH_STATE_INVALID
    assert a2.metadata["search_state"] == SEARCH_STATE_INVALID


def test_repair_is_idempotent(client, artifact_registry, ctx):
    """Second call invalidates zero — the first pass already stamped
 them."""
    _seed_orphan_artifact(
        artifact_registry, ctx,
        artifact_id="orphan-1", document_id="doc-1",
    )
    first = client.post("/documents/doc-1/repair", headers=_headers(ctx))
    second = client.post("/documents/doc-1/repair", headers=_headers(ctx))
    assert first.json()["data"]["invalidatedArtifactCount"] == 1
    assert second.json()["data"]["invalidatedArtifactCount"] == 0


def test_repair_only_touches_target_document(
    client, artifact_registry, ctx,
):
    """Orphans for OTHER documents must not be invalidated when we
 repair doc-1 — different documents, different reindex
 lifecycles."""
    _seed_orphan_artifact(
        artifact_registry, ctx,
        artifact_id="orphan-1", document_id="doc-1",
    )
    _seed_orphan_artifact(
        artifact_registry, ctx,
        artifact_id="other-1", document_id="doc-other",
    )

    resp = client.post("/documents/doc-1/repair", headers=_headers(ctx))

    assert resp.json()["data"]["invalidatedArtifactCount"] == 1
    a_other = artifact_registry.get(ctx, "other-1")
    assert "search_state" not in a_other.metadata


def test_repair_returns_zero_when_no_orphans(client, ctx):
    """Document with no orphan artifacts → invalidatedArtifactCount=0,
 not 404. The repair tool is idempotent and tolerant of clean
 corpora."""
    resp = client.post("/documents/doc-clean/repair", headers=_headers(ctx))
    assert resp.status_code == 200
    assert resp.json()["data"]["invalidatedArtifactCount"] == 0


def test_repair_skips_artifacts_that_have_run_id(
    client, artifact_registry, ctx,
):
    """A correctly-stamped artifact (has run_id) is NOT an orphan —
 the sweep must leave it alone."""
    artifact_registry.add(ArtifactRecord(
        artifact_id="legitimate",
        project=ctx, kind="graph_json",
        location="graph/legitimate.json",
        content_hash="sha256:legitimate", byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-1"], source_artifact_ids=[],
        metadata={"run_id": "run-good"},
    ))
    resp = client.post("/documents/doc-1/repair", headers=_headers(ctx))
    assert resp.json()["data"]["invalidatedArtifactCount"] == 0
    art = artifact_registry.get(ctx, "legitimate")
    assert "search_state" not in art.metadata
