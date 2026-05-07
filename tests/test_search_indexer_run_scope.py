"""Tests for run-scoped search + server-derived chunk_id / run_id.

The validation feature (Phase 1) needs the search index to:

1. Carry `run_id` and `chunk_id` columns populated from the indexed
   artifact's metadata, NOT from any client-supplied or LLM-supplied
   value (the trust-rule decision in the implementation plan).
2. Honour a `RunScope(run_id=…)` filter that lives in the SQL WHERE
   clause — BM25 must rank only the rows that survived the filter,
   not be applied post-topK (which would distort scores).
3. Preserve the legacy unscoped behaviour byte-for-byte when the
   caller passes `WorkspaceScope()`, `None`, or omits the parameter.

These tests are the lock that keeps the run-scoping contract honest
across future indexer changes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.query.scope import RunScope, WorkspaceScope, default_scope
from j1.search import SqliteSearchIndexer
from j1.workspace.layout import WorkspaceArea


# ---- Helpers -------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stage(
    workspace,
    ctx,
    artifact_registry,
    *,
    artifact_id: str,
    content: bytes,
    kind: str = "chunk",
    run_id: str | None = None,
    chunk_id: str | None = None,
    source_document_ids: list[str] | None = None,
) -> ArtifactRecord:
    """Write an artifact file + register the record. Keeps tests
    independent of the production registration path so we exercise the
    indexer in isolation."""
    area = WorkspaceArea.COMPILED
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.txt"
    (area_dir / stored).write_bytes(content)
    metadata: dict = {}
    if run_id:
        metadata["run_id"] = run_id
    if chunk_id:
        metadata["chunk_id"] = chunk_id
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
        source_document_ids=source_document_ids or [],
        metadata=metadata,
    )
    artifact_registry.add(record)
    return record


@pytest.fixture
def indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


# ---- Server-derived run_id / chunk_id ------------------------------


def test_search_hit_carries_run_id_when_metadata_set(
    indexer, workspace, ctx, artifact_registry,
):
    """run_id round-trips: the column is populated from
    `metadata.run_id` at index time and surfaces on every SearchHit
    that matched the FTS query."""
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"hello world",
        run_id="run-abc", chunk_id="chunk-001",
    )
    indexer.index(ctx, ["a-1"])

    hit = indexer.search(ctx, "hello")[0]
    assert hit.run_id == "run-abc"
    assert hit.chunk_id == "chunk-001"


def test_search_hit_carries_none_when_metadata_absent(
    indexer, workspace, ctx, artifact_registry,
):
    """No producer-supplied run_id / chunk_id → the columns are
    stored empty and the DTO surfaces None. The empty-string-to-None
    coercion lives in `_row_to_hit`; without it the FE would render
    a literal empty string in citation labels."""
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"hello", kind="compiled.text",
    )
    indexer.index(ctx, ["a-1"])

    hit = indexer.search(ctx, "hello")[0]
    assert hit.run_id is None
    assert hit.chunk_id is None


def test_chunk_id_independent_of_artifact_id(
    indexer, workspace, ctx, artifact_registry,
):
    """The artifact's chunk artifact_id (registry primary key) and the
    canonical chunk_id (LightRAG-assigned identifier the FE displays)
    are different things. The indexer must keep them distinct."""
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="art-internal-uuid", content=b"chunk body text",
        run_id="run-1", chunk_id="chunk-publicly-shareable-id",
    )
    indexer.index(ctx, ["art-internal-uuid"])

    hit = indexer.search(ctx, "body")[0]
    assert hit.artifact_id == "art-internal-uuid"
    assert hit.chunk_id == "chunk-publicly-shareable-id"


# ---- RunScope filter -----------------------------------------------


def test_run_scope_filters_to_target_run_only(
    indexer, workspace, ctx, artifact_registry,
):
    """Two runs index artifacts that share a search term. RunScope
    must restrict results to the requested run."""
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-run-A", content=b"shared keyword apple",
        run_id="run-A", chunk_id="chunk-A1",
    )
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-run-B", content=b"shared keyword banana",
        run_id="run-B", chunk_id="chunk-B1",
    )
    indexer.index(ctx, ["a-run-A", "a-run-B"])

    only_a = indexer.search(ctx, "shared", scope=RunScope(run_id="run-A"))
    only_b = indexer.search(ctx, "shared", scope=RunScope(run_id="run-B"))

    assert {h.artifact_id for h in only_a} == {"a-run-A"}
    assert {h.artifact_id for h in only_b} == {"a-run-B"}
    # Each hit's run_id is server-derived from the matched row, never
    # echoed from the request — tested explicitly to lock the trust
    # rule into place.
    assert only_a[0].run_id == "run-A"
    assert only_b[0].run_id == "run-B"


def test_run_scope_with_unknown_run_returns_empty(
    indexer, workspace, ctx, artifact_registry,
):
    """A scope that points at a run with no indexed artifacts must
    return an empty list, not silently widen to the project."""
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"hello world", run_id="run-A",
    )
    indexer.index(ctx, ["a-1"])

    assert indexer.search(ctx, "hello", scope=RunScope(run_id="run-Z")) == []


def test_run_scope_filters_before_ranking(
    indexer, workspace, ctx, artifact_registry,
):
    """Critical: BM25 ranking must see only the run-A rows when
    scoped to run-A, not be applied post-topK. We verify by indexing
    a run-B row that would dominate the BM25 score on the global
    index (it contains the search term with the strongest density),
    then confirming it never appears in run-A's scoped results even
    though `max_results=10` is wider than run-A's count."""
    # Two run-A rows with low keyword density.
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-run-A-1", content=b"alpha alpha filler filler filler",
        run_id="run-A",
    )
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-run-A-2", content=b"alpha filler filler filler filler",
        run_id="run-A",
    )
    # One run-B row that would BM25-dominate without the filter
    # (very high keyword density).
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-run-B-DOMINANT", content=b"alpha alpha alpha alpha alpha",
        run_id="run-B",
    )
    indexer.index(ctx, ["a-run-A-1", "a-run-A-2", "a-run-B-DOMINANT"])

    scoped = indexer.search(
        ctx, "alpha", scope=RunScope(run_id="run-A"), max_results=10,
    )
    assert {h.artifact_id for h in scoped} == {"a-run-A-1", "a-run-A-2"}
    # Sanity: without the scope, the run-B row IS the top hit.
    unscoped = indexer.search(ctx, "alpha", max_results=10)
    assert unscoped[0].artifact_id == "a-run-B-DOMINANT"


def test_workspace_scope_unchanged_from_default(
    indexer, workspace, ctx, artifact_registry,
):
    """`WorkspaceScope()`, `default_scope()`, and `scope=None` (the
    legacy keyword-omitted path) must produce byte-identical result
    sets. This is the regression lock for every existing /search,
    /retrieve, /answer caller."""
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"alpha shared", run_id="run-A",
    )
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-2", content=b"beta shared", run_id="run-B",
    )
    indexer.index(ctx, ["a-1", "a-2"])

    no_scope = indexer.search(ctx, "shared")
    workspace_scope = indexer.search(ctx, "shared", scope=WorkspaceScope())
    default = indexer.search(ctx, "shared", scope=default_scope())

    ids_no = [h.artifact_id for h in no_scope]
    ids_ws = [h.artifact_id for h in workspace_scope]
    ids_def = [h.artifact_id for h in default]
    assert ids_no == ids_ws == ids_def


def test_run_scope_combines_with_artifact_type_filter(
    indexer, workspace, ctx, artifact_registry,
):
    """artifact_types and scope are independent filters; both must
    AND together. Validation needs this — chunk-only retrieval
    inside a single run is the typical search pattern."""
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="chunk-A", content=b"shared keyword",
        kind="chunk", run_id="run-1", chunk_id="c-A",
    )
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="other-A", content=b"shared keyword",
        kind="enriched.tables", run_id="run-1",
    )
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="chunk-B", content=b"shared keyword",
        kind="chunk", run_id="run-2", chunk_id="c-B",
    )
    indexer.index(ctx, ["chunk-A", "other-A", "chunk-B"])

    hits = indexer.search(
        ctx, "shared",
        artifact_types=["chunk"],
        scope=RunScope(run_id="run-1"),
    )
    assert {h.artifact_id for h in hits} == {"chunk-A"}


# ---- Backward compatibility ----------------------------------------


def test_legacy_callers_omitting_scope_param_unchanged(
    indexer, workspace, ctx, artifact_registry,
):
    """Locks the contract for every existing call site (/search,
    /retrieve, /answer, the test suites). Omitting the keyword
    keeps the historical behaviour."""
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"hello", run_id="run-X",
    )
    _stage(
        workspace, ctx, artifact_registry,
        artifact_id="a-2", content=b"hello", run_id="run-Y",
    )
    indexer.index(ctx, ["a-1", "a-2"])

    hits = indexer.search(ctx, "hello", max_results=10)
    assert {h.artifact_id for h in hits} == {"a-1", "a-2"}
