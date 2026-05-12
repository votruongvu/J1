"""Lineage-hardening regression tests.

Locks in the four guards/sweeps added in the lineage-hardening
round (after the document-centric refactor): fail-fast on
lineage-required artifact writes, search_state filter behavior,
the post-promotion supersede sweep, and the pre-reindex orphan
invalidation. Each test maps to a specific failure mode the
validation reports exposed:

  * graph_json with run_id=None       → fail-fast guard catches it
  * superseded artifacts still in retrieval after reindex
                                      → supersede sweep
  * pre-existing orphans poisoning new run
                                      → invalidate orphan sweep
  * "Not in retrieved evidence" with no diagnostic info
                                      → debug fields populated
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactNotFoundError
from j1.documents.artifact_state import (
    SEARCH_STATE_ACTIVE,
    SEARCH_STATE_INVALID,
    SEARCH_STATE_SUPERSEDED,
    invalidate_orphan_artifacts,
    supersede_previous_active_artifacts,
)
from j1.documents.lifecycle import (
    filter_to_attached_artifacts,
    is_attached,
)
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.knowledge import (
    LineageError,
    _enforce_lineage_or_raise,
)
from j1.projects.context import ProjectContext


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


# ---- In-memory test registry ----------------------------------


class _InMemoryArtifactRegistry:
    """Mirrors the protocol's read/write surface — just enough for
 these tests to exercise the stamping helpers without a workspace
 fixture."""

    def __init__(self):
        self._records: list[ArtifactRecord] = []

    def add(self, record):
        self._records.append(record)

    def get(self, ctx, artifact_id):
        for r in self._records:
            if r.artifact_id == artifact_id:
                return r
        raise ArtifactNotFoundError(artifact_id)

    def list_artifacts(self, ctx, *, kind=None):
        if kind is None:
            return list(self._records)
        return [r for r in self._records if r.kind == kind]

    def update_metadata(self, ctx, artifact_id, metadata):
        from dataclasses import replace
        for i, r in enumerate(self._records):
            if r.artifact_id == artifact_id:
                self._records[i] = replace(r, metadata=dict(metadata))
                return
        raise ArtifactNotFoundError(artifact_id)


def _artifact(
    *, ctx, artifact_id: str, kind: str = "chunk",
    metadata: dict | None = None,
    source_document_ids: list[str] | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"area/{artifact_id}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=source_document_ids or ["doc-1"],
        source_artifact_ids=[],
        metadata=metadata or {},
    )


# ============================================================
# A: fail-fast guard on artifact writes
# ============================================================


def test_lineage_guard_rejects_graph_json_without_run_id():
    """The headline regression: graph_json with run_id=None must
 be refused at write time. Previously the orchestration activity
 silently wrote it, the search indexer picked it up, and
 validation later flagged the lineage mismatch hours later."""
    with pytest.raises(LineageError, match="run_id"):
        _enforce_lineage_or_raise("graph_json", {}, "art-1")


def test_lineage_guard_rejects_chunk_without_run_id():
    with pytest.raises(LineageError, match="run_id"):
        _enforce_lineage_or_raise("chunk", {}, "art-1")


def test_lineage_guard_rejects_compiled_text_without_run_id():
    with pytest.raises(LineageError, match="run_id"):
        _enforce_lineage_or_raise("compiled.text", {}, "art-1")


def test_lineage_guard_rejects_enriched_kinds_without_run_id():
    """All enriched.* artifact kinds are validation-scoped — every
 one must carry run_id at write time."""
    for kind in (
        "enriched.tables", "enriched.visuals", "enriched.document_map",
        "enriched.requirements", "enriched.formulas",
    ):
        with pytest.raises(LineageError):
            _enforce_lineage_or_raise(kind, {}, "art-1")


def test_lineage_guard_allows_lineage_required_kinds_with_run_id():
    """Happy path: a graph_json with run_id stamped passes the guard."""
    _enforce_lineage_or_raise(
        "graph_json", {"run_id": "run-abc"}, "art-1",
    )


def test_lineage_guard_ignores_non_required_kinds():
    """Generic blob-style kinds (e.g. uploaded reports) legitimately
 have no run context. The guard must NOT block them."""
    # No exception expected.
    _enforce_lineage_or_raise("operator_upload", {}, "art-1")
    _enforce_lineage_or_raise("unknown_future_kind", {}, "art-1")


def test_lineage_guard_handles_none_metadata():
    """Defensive: a None metadata dict shouldn't crash the guard."""
    with pytest.raises(LineageError):
        _enforce_lineage_or_raise("graph_json", None, "art-1")  # type: ignore[arg-type]


# ============================================================
# search_state filter behavior
# ============================================================


def test_filter_drops_superseded_artifacts(ctx):
    """The post-promotion supersede sweep stamps old artifacts with
 `search_state=superseded`. Retrieval must drop them — they're
 from the previous run that's no longer active."""
    keep = _artifact(ctx=ctx, artifact_id="keep")
    drop = _artifact(
        ctx=ctx, artifact_id="drop",
        metadata={"search_state": SEARCH_STATE_SUPERSEDED},
    )
    out = filter_to_attached_artifacts([keep, drop])
    assert [a.artifact_id for a in out] == ["keep"]


def test_filter_drops_invalid_artifacts(ctx):
    """Orphans invalidated by the pre-reindex sweep must not reach
 retrieval. They stay on disk for audit; the filter just hides
 them."""
    keep = _artifact(ctx=ctx, artifact_id="keep")
    orphan = _artifact(
        ctx=ctx, artifact_id="orphan",
        metadata={"search_state": SEARCH_STATE_INVALID},
    )
    assert filter_to_attached_artifacts([keep, orphan]) == [keep]


def test_filter_requires_both_knowledge_state_and_search_state(ctx):
    """The visibility check is an AND — detached (operator) OR
 superseded (system) both hide the artifact."""
    detached = _artifact(
        ctx=ctx, artifact_id="detached",
        metadata={"knowledge_state": "detached"},
    )
    superseded = _artifact(
        ctx=ctx, artifact_id="superseded",
        metadata={"search_state": "superseded"},
    )
    visible = _artifact(ctx=ctx, artifact_id="visible")
    out = filter_to_attached_artifacts([detached, superseded, visible])
    assert [a.artifact_id for a in out] == ["visible"]


def test_filter_explicit_active_passes(ctx):
    """An artifact explicitly stamped `search_state=active` is
 visible — same as the missing-field default."""
    rec = _artifact(
        ctx=ctx, artifact_id="rec",
        metadata={"search_state": SEARCH_STATE_ACTIVE},
    )
    assert is_attached(rec) is True


# ============================================================
# C: supersede_previous_active_artifacts
# ============================================================


def test_supersede_stamps_only_previous_run_artifacts(ctx):
    """The hook flips search_state on the PREVIOUS active run's
 artifacts — never the new run's, never other documents'."""
    registry = _InMemoryArtifactRegistry()
    # Previous active run: 2 artifacts on doc-1.
    registry.add(_artifact(
        ctx=ctx, artifact_id="prev-1",
        metadata={"run_id": "r-old"},
    ))
    registry.add(_artifact(
        ctx=ctx, artifact_id="prev-2",
        metadata={"run_id": "r-old"},
    ))
    # New active run: 1 artifact on doc-1. Must NOT be stamped.
    registry.add(_artifact(
        ctx=ctx, artifact_id="new-1",
        metadata={"run_id": "r-new"},
    ))
    # Unrelated artifact on a different document.
    registry.add(_artifact(
        ctx=ctx, artifact_id="other",
        metadata={"run_id": "r-old"},
        source_document_ids=["doc-other"],
    ))

    stamped = supersede_previous_active_artifacts(
        ctx=ctx, artifacts=registry,
        document_id="doc-1",
        new_run_id="r-new",
        previous_run_id="r-old",
    )

    assert stamped == 2
    assert registry.get(ctx, "prev-1").metadata["search_state"] == SEARCH_STATE_SUPERSEDED
    assert registry.get(ctx, "prev-1").metadata["superseded_by_run_id"] == "r-new"
    assert registry.get(ctx, "prev-2").metadata["search_state"] == SEARCH_STATE_SUPERSEDED
    # New run + other-document artifacts untouched.
    assert "search_state" not in registry.get(ctx, "new-1").metadata
    assert "search_state" not in registry.get(ctx, "other").metadata


def test_supersede_is_idempotent(ctx):
    """Re-running the supersede sweep doesn't re-stamp already-
 superseded artifacts. Important: the workflow's continue-as-new
 boundary can re-fire the promotion hook, and we don't want it
 to bump audit churn."""
    registry = _InMemoryArtifactRegistry()
    registry.add(_artifact(
        ctx=ctx, artifact_id="prev",
        metadata={
            "run_id": "r-old",
            "search_state": SEARCH_STATE_SUPERSEDED,
            "superseded_by_run_id": "r-new",
        },
    ))
    stamped = supersede_previous_active_artifacts(
        ctx=ctx, artifacts=registry,
        document_id="doc-1",
        new_run_id="r-new",
        previous_run_id="r-old",
    )
    assert stamped == 0


def test_supersede_noop_when_no_previous_active_run(ctx):
    """First run for a document has no previous active to
 supersede. Returns 0; doesn't crash."""
    registry = _InMemoryArtifactRegistry()
    registry.add(_artifact(
        ctx=ctx, artifact_id="only",
        metadata={"run_id": "r-1"},
    ))
    stamped = supersede_previous_active_artifacts(
        ctx=ctx, artifacts=registry,
        document_id="doc-1",
        new_run_id="r-1",
        previous_run_id=None,
    )
    assert stamped == 0
    assert "search_state" not in registry.get(ctx, "only").metadata


def test_supersede_skips_when_same_run_re_promoted(ctx):
    """Same run promoting itself (continue-as-new boundary) is
 a no-op — we'd be marking the new run's own artifacts as
 superseded, which would defeat the purpose."""
    registry = _InMemoryArtifactRegistry()
    registry.add(_artifact(
        ctx=ctx, artifact_id="art",
        metadata={"run_id": "r-1"},
    ))
    stamped = supersede_previous_active_artifacts(
        ctx=ctx, artifacts=registry,
        document_id="doc-1",
        new_run_id="r-1",
        previous_run_id="r-1",
    )
    assert stamped == 0


# ============================================================
# B: invalidate_orphan_artifacts
# ============================================================


def test_invalidate_orphans_stamps_missing_run_id(ctx):
    """The pre-reindex repair: orphan artifacts (run_id=None)
 tied to the target document get `search_state=invalid`. They
 stay on disk; the filter just hides them from retrieval."""
    registry = _InMemoryArtifactRegistry()
    registry.add(_artifact(
        ctx=ctx, artifact_id="orphan",
        metadata={},  # no run_id
    ))
    registry.add(_artifact(
        ctx=ctx, artifact_id="legitimate",
        metadata={"run_id": "r-1"},
    ))
    stamped = invalidate_orphan_artifacts(
        ctx=ctx, artifacts=registry, document_id="doc-1",
    )
    assert stamped == 1
    assert registry.get(ctx, "orphan").metadata["search_state"] == SEARCH_STATE_INVALID
    assert registry.get(ctx, "orphan").metadata["invalid_reason"] == "missing_run_id"
    # Legitimate artifact untouched.
    assert "search_state" not in registry.get(ctx, "legitimate").metadata


def test_invalidate_orphans_only_targets_matching_document(ctx):
    """Orphans for OTHER documents must not be touched. The reindex
 of doc-1 has no business invalidating doc-2's data."""
    registry = _InMemoryArtifactRegistry()
    registry.add(_artifact(
        ctx=ctx, artifact_id="orphan-other",
        metadata={},
        source_document_ids=["doc-other"],
    ))
    stamped = invalidate_orphan_artifacts(
        ctx=ctx, artifacts=registry, document_id="doc-1",
    )
    assert stamped == 0
    assert "search_state" not in registry.get(ctx, "orphan-other").metadata


def test_invalidate_orphans_is_idempotent(ctx):
    """Already-invalidated orphans don't re-stamp on a second
 pass. Lets the reindex endpoint call the sweep without
 worrying about idempotency."""
    registry = _InMemoryArtifactRegistry()
    registry.add(_artifact(
        ctx=ctx, artifact_id="orphan",
        metadata={
            "search_state": SEARCH_STATE_INVALID,
            "invalid_reason": "missing_run_id",
        },
    ))
    stamped = invalidate_orphan_artifacts(
        ctx=ctx, artifacts=registry, document_id="doc-1",
    )
    assert stamped == 0


# ============================================================
# D: validation debug fields
# ============================================================


def test_debug_dict_populated_on_no_retrieval():
    """When retrieval found nothing, `fallback_reason="no_retrieval"`
 surfaces so the FE can render "no chunks matched your query"
 instead of a vague "Not in retrieved evidence"."""
    from j1.validation.service import _build_manual_query_debug
    debug = _build_manual_query_debug(
        retrieved=[],
        evidence_blocks=[],
        synthesized_answer=None,
        llm_trace=None,
    )
    assert debug["retrieved_count"] == 0
    assert debug["evidence_items_after_filter"] == 0
    assert debug["fallback_reason"] == "synthesis_disabled"


def test_debug_dict_classifies_llm_abstention():
    """When retrieval + evidence both have content but the LLM
 still abstained, fallback_reason="llm_abstained" — the most
 actionable signal for "the model didn't ground in the evidence
 we gave it"."""
    from j1.validation.dtos import EvidenceBlockDTO, LLMTraceDTO, RetrievedChunkRefDTO
    from j1.validation.service import _build_manual_query_debug
    debug = _build_manual_query_debug(
        retrieved=[RetrievedChunkRefDTO(
            artifact_id="a-1", chunk_id="c-1", run_id="r-1",
            document_id="d-1", source_location=None,
            score=1.0, preview="...", artifact_kind="chunk",
        )],
        evidence_blocks=[EvidenceBlockDTO(
            artifact_id="a-1", artifact_type="chunk",
            text="The proposal is due 20 May 2026.",
        )],
        synthesized_answer=None,
        llm_trace=LLMTraceDTO(called=True, error=None),
    )
    assert debug["fallback_reason"] == "llm_abstained"


def test_debug_dict_classifies_llm_error():
    from j1.validation.dtos import EvidenceBlockDTO, LLMTraceDTO, RetrievedChunkRefDTO
    from j1.validation.service import _build_manual_query_debug
    debug = _build_manual_query_debug(
        retrieved=[RetrievedChunkRefDTO(
            artifact_id="a-1", chunk_id="c-1", run_id="r-1",
            document_id="d-1", source_location=None,
            score=1.0, preview="x", artifact_kind="chunk",
        )],
        evidence_blocks=[EvidenceBlockDTO(
            artifact_id="a-1", artifact_type="chunk", text="x",
        )],
        synthesized_answer=None,
        llm_trace=LLMTraceDTO(called=True, error="timeout"),
    )
    assert debug["fallback_reason"] == "llm_error"


def test_debug_dict_collects_artifact_types():
    """Operator visibility: 'graph_json dominated my retrieval'
 should be obvious from the debug payload. `artifact_types_before/
 after_filter` exposes the kinds in play."""
    from j1.validation.dtos import EvidenceBlockDTO, RetrievedChunkRefDTO
    from j1.validation.service import _build_manual_query_debug
    debug = _build_manual_query_debug(
        retrieved=[
            RetrievedChunkRefDTO(
                artifact_id="a-1", chunk_id=None, run_id="r-1",
                document_id="d-1", source_location=None,
                score=1.0, preview="x", artifact_kind="graph_json",
            ),
            RetrievedChunkRefDTO(
                artifact_id="a-2", chunk_id="c-1", run_id="r-1",
                document_id="d-1", source_location=None,
                score=0.9, preview="y", artifact_kind="chunk",
            ),
        ],
        evidence_blocks=[EvidenceBlockDTO(
            artifact_id="a-2", artifact_type="chunk", text="y body",
        )],
        synthesized_answer="some answer",
        llm_trace=None,
    )
    assert "graph_json" in debug["artifact_types_before_filter"]
    assert "chunk" in debug["artifact_types_before_filter"]
    assert debug["artifact_types_after_filter"] == ["chunk"]
    assert debug["fallback_reason"] is None  # synthesis succeeded
    assert debug["top_evidence_preview"].startswith("y body")
    assert debug["total_context_chars"] == len("y body")


def test_debug_dict_populated_in_response_envelope():
    """End-to-end: the response actually carries the debug dict
 after building. Lighter integration test that the field reached
 the DTO."""
    from j1.validation.dtos import ManualTestQueryResponseDTO
    dto = ManualTestQueryResponseDTO(
        request_id="r", run_id="run", question="?", answer="",
        mode_used="auto", retrieved_chunks=[], citations=[],
        checks=[], validation_status="passed", debug={"fallback_reason": "no_retrieval"},
    )
    assert dto.debug["fallback_reason"] == "no_retrieval"


# ============================================================
# Integration: filter + supersede chain
# ============================================================


def test_supersede_then_filter_excludes_previous_run_artifacts(ctx):
    """End-to-end: after a successful reindex promotes a new run,
 the previous run's artifacts get stamped superseded and disappear
 from retrieval results. Locks the spec's "after reindex, normal
 retrieval should only return artifacts from the active run"
 rule."""
    registry = _InMemoryArtifactRegistry()
    prev_artifact = _artifact(
        ctx=ctx, artifact_id="prev",
        metadata={"run_id": "r-old"},
    )
    new_artifact = _artifact(
        ctx=ctx, artifact_id="new",
        metadata={"run_id": "r-new"},
    )
    registry.add(prev_artifact)
    registry.add(new_artifact)

    # 1. Both visible initially.
    visible_before = filter_to_attached_artifacts(registry.list_artifacts(ctx))
    assert {a.artifact_id for a in visible_before} == {"prev", "new"}

    # 2. Promotion hook fires: supersede the previous active.
    supersede_previous_active_artifacts(
        ctx=ctx, artifacts=registry,
        document_id="doc-1",
        new_run_id="r-new",
        previous_run_id="r-old",
    )

    # 3. Only the new run's artifact survives the filter.
    visible_after = filter_to_attached_artifacts(registry.list_artifacts(ctx))
    assert {a.artifact_id for a in visible_after} == {"new"}
