"""Phase 2: ingest-trace events for the enrichment-alias producer.

``ProcessingActivities._maybe_emit_enrichment_aliases`` is a
best-effort post-step. Operators investigating why a document has
no enrichment aliases need to know whether the producer ran,
skipped (and why), or emitted zero aliases. This pin asserts the
``ingest.alias_producer.*`` trace surface stays stable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.observability.ingest_trace import (
    IngestTraceLogger,
    IngestTraceSettings,
    reset_ingest_trace_logger,
)
from j1.orchestration.activities.processing import ProcessingActivities
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def trace_path(tmp_path: Path) -> Path:
    return tmp_path / "alias_trace.jsonl"


@pytest.fixture(autouse=True)
def _install_trace_logger(trace_path: Path):
    logger = IngestTraceLogger(IngestTraceSettings(
        enabled=True, output_path=str(trace_path), slow_stage_ms=10_000,
    ))
    reset_ingest_trace_logger(logger)
    yield
    reset_ingest_trace_logger(None)


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines() if line.strip()
    ]


def _events_for_stage(path: Path, stage: str) -> list[dict]:
    return [e for e in _read_events(path) if e.get("stage") == stage]


def _seed_doc(registry, ctx, *, document_id: str, snapshot_id: str | None):
    registry.add(DocumentRecord(
        document_id=document_id, project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf", file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED, created_at=_NOW,
        knowledge_state="attached",
        active_snapshot_id=snapshot_id,
    ))


def _seed_run(run_store, ctx, *, run_id: str, document_id: str,
              target_snapshot_id: str | None):
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id, document_id=document_id,
        workflow_id=f"wf-{run_id}", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW, completed_at=_NOW,
        metadata={}, run_type="initial",
        target_snapshot_id=target_snapshot_id,
    ))


def _seed_chunk(artifact_registry, ctx, *, artifact_id: str,
                document_id: str, snapshot_id: str, body: str):
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id, project=ctx,
        kind="chunk",
        location=f"chunks/{artifact_id}.json",
        content_hash=f"sha256:{artifact_id}", byte_size=len(body),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=[document_id],
        metadata={"snapshot_id": snapshot_id, "body": body},
        snapshot_id=snapshot_id,
        created_by_run_id="r-baseline",
    ))


def _make_activities(workspace, registry, artifact_registry, run_store):
    return ProcessingActivities(
        processing=None,  # type: ignore[arg-type]
        sources=registry,
        artifacts=artifact_registry,
        run_store=run_store,
    )


def test_alias_producer_emits_skipped_when_run_store_unwired(
    workspace, registry, artifact_registry, ctx, trace_path,
):
    """Without a run store there's no snapshot to scope to. Pin the
    `skipped` trace so operators can see the no-op reason."""
    acts = ProcessingActivities(
        processing=None,  # type: ignore[arg-type]
        sources=registry,
        artifacts=artifact_registry,
        run_store=None,
    )
    out = acts._maybe_emit_enrichment_aliases(
        ctx, run_id="r-1", document_id="doc-1", actor="test",
    )
    assert out is None
    [evt] = _events_for_stage(trace_path, "alias_producer")
    assert evt["trace_event"] == "ingest.alias_producer.skipped"
    assert evt["status"] == "skipped"
    assert evt["metadata"]["reason"] == "run_store_unwired"


def test_alias_producer_emits_skipped_when_run_missing_snapshot(
    workspace, registry, artifact_registry, ctx, trace_path,
):
    """A run record with no `target_snapshot_id` cannot stamp an
    alias artifact. Pin the skip-reason."""
    run_store = JsonlIngestionRunStore(workspace)
    _seed_doc(registry, ctx, document_id="doc-1", snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1",
              target_snapshot_id=None)
    acts = _make_activities(workspace, registry, artifact_registry, run_store)
    out = acts._maybe_emit_enrichment_aliases(
        ctx, run_id="r-1", document_id="doc-1", actor="test",
    )
    assert out is None
    [evt] = _events_for_stage(trace_path, "alias_producer")
    assert evt["metadata"]["reason"] == "no_target_snapshot"


def test_alias_producer_emits_skipped_when_no_chunks_in_scope(
    workspace, registry, artifact_registry, ctx, trace_path,
):
    """Run has a snapshot but no chunk artifacts ever landed under
    that snapshot. Surface the `no_chunks` reason."""
    run_store = JsonlIngestionRunStore(workspace)
    _seed_doc(registry, ctx, document_id="doc-1", snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1",
              target_snapshot_id="snap-active")
    acts = _make_activities(workspace, registry, artifact_registry, run_store)
    out = acts._maybe_emit_enrichment_aliases(
        ctx, run_id="r-1", document_id="doc-1", actor="test",
    )
    assert out is None
    [evt] = _events_for_stage(trace_path, "alias_producer")
    assert evt["metadata"]["reason"] == "no_chunks"
    assert evt["metadata"]["chunk_count"] == 0


def test_alias_producer_emits_completed_zero_when_chunks_yield_no_aliases(
    workspace, registry, artifact_registry, ctx, trace_path,
):
    """Chunks exist but the extractor finds no alias-shaped patterns.
    A `completed` event with `alias_count=0` lets operators see that
    the producer ran (so absence of aliases is a content problem,
    not a wiring problem)."""
    run_store = JsonlIngestionRunStore(workspace)
    _seed_doc(registry, ctx, document_id="doc-1", snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1",
              target_snapshot_id="snap-active")
    _seed_chunk(
        artifact_registry, ctx, artifact_id="a-1",
        document_id="doc-1", snapshot_id="snap-active",
        body="Plain prose with no acronym definitions whatsoever.",
    )
    acts = _make_activities(workspace, registry, artifact_registry, run_store)
    out = acts._maybe_emit_enrichment_aliases(
        ctx, run_id="r-1", document_id="doc-1", actor="test",
    )
    assert out is None
    events = _events_for_stage(trace_path, "alias_producer")
    assert [e["trace_event"] for e in events] == [
        "ingest.alias_producer.started",
        "ingest.alias_producer.completed",
    ]
    completed = events[1]
    assert completed["metadata"]["alias_count"] == 0
    assert completed["metadata"]["persisted"] is False


def test_alias_producer_emits_completed_with_alias_count(
    workspace, registry, artifact_registry, ctx, trace_path,
):
    """Happy path: extractor finds aliases, producer persists them
    under the run's snapshot, completed event carries `alias_count
    > 0` and `persisted=true`. Pinned so future refactors keep the
    observable signal."""
    run_store = JsonlIngestionRunStore(workspace)
    _seed_doc(registry, ctx, document_id="doc-1", snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1",
              target_snapshot_id="snap-active")
    _seed_chunk(
        artifact_registry, ctx, artifact_id="a-1",
        document_id="doc-1", snapshot_id="snap-active",
        body="Reference the bill of quantities (BOQ) before each cycle.",
    )
    acts = _make_activities(workspace, registry, artifact_registry, run_store)
    out = acts._maybe_emit_enrichment_aliases(
        ctx, run_id="r-1", document_id="doc-1", actor="test",
    )
    assert out is not None
    events = _events_for_stage(trace_path, "alias_producer")
    assert [e["trace_event"] for e in events] == [
        "ingest.alias_producer.started",
        "ingest.alias_producer.completed",
    ]
    completed = events[1]
    assert completed["metadata"]["alias_count"] >= 1
    assert completed["metadata"]["persisted"] is True
    assert completed["metadata"]["artifact_id"] == out
    # Snapshot context propagates onto every event.
    for e in events:
        assert e["target_snapshot_id"] == "snap-active"
        assert e["run_id"] == "r-1"
        assert e["document_id"] == "doc-1"
