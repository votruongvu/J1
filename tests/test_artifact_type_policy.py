"""Artifact-type policy tests.

Spec section 7 rule: textual evidence must win the synthesizer's
budget. `graph_json` and `enriched.formulas` exist in the
retrieval result set but must never crowd out chunk / compiled.text
even when the engine scored them higher.

The policy lives in `j1.validation.evidence._kind_priority` — these
tests pin the ordering so a future "let's adjust the priorities"
change has to update the test names explicitly (and confront the
matrix).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactNotFoundError
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.validation.dtos import RetrievedChunkRefDTO
from j1.validation.evidence import (
    _DEFAULT_KIND_PRIORITY,
    _KIND_PRIORITY,
    _kind_priority,
    build_evidence_blocks,
)


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


class _StubRegistry:
    """Minimal artifact registry. Returns canned bodies via
 `path_resolver` rather than reading the disk — keeps these tests
 file-system-free and fast."""

    def __init__(self, records: list[ArtifactRecord]):
        self._records = {r.artifact_id: r for r in records}

    def get(self, ctx, artifact_id):
        if artifact_id not in self._records:
            raise ArtifactNotFoundError(artifact_id)
        return self._records[artifact_id]


def _artifact(
    *, ctx: ProjectContext, artifact_id: str, kind: str,
    location: str,
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=location,
        content_hash=f"sha256:{artifact_id}",
        byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata={},
    )


def _hit(
    *, artifact_id: str, kind: str,
    chunk_id: str | None = None,
    score: float = 1.0,
    preview: str = "",
) -> RetrievedChunkRefDTO:
    return RetrievedChunkRefDTO(
        artifact_id=artifact_id,
        chunk_id=chunk_id,
        run_id="r-1",
        document_id="doc-1",
        source_location=None,
        score=score,
        preview=preview,
        artifact_kind=kind,
    )


def _write_chunk_ndjson(path: Path, *, chunks: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c) + "\n")


# ---- Priority ordering ------------------------------------------


def test_chunk_outranks_compiled_text():
    """Spec rule: `chunk` is the canonical ground truth — must
 always come before `compiled.text` for evidence budgeting."""
    assert _kind_priority("chunk") < _kind_priority("compiled.text")


def test_textual_kinds_outrank_graph_json():
    """The headline rule: graph_json never beats textual kinds for
 the synthesizer's context. Pin this explicitly so a future
 priority shuffle has to update the test."""
    assert _kind_priority("chunk") < _kind_priority("graph_json")
    assert _kind_priority("compiled.text") < _kind_priority("graph_json")
    assert _kind_priority("parsed_content_manifest") < _kind_priority("graph_json")


def test_textual_kinds_outrank_formulas():
    """Same rule for `enriched.formulas` — equation blobs don't
 belong in textual answer synthesis."""
    assert _kind_priority("chunk") < _kind_priority("enriched.formulas")


def test_unknown_kinds_fall_in_middle_of_pack():
    """A future artifact kind (e.g. `enriched.entities`) gets a
 middle-of-pack priority. Better than failing silently or
 deprioritizing to the bottom — at least the synthesizer can
 see it after the canonical textual kinds.

 ``enriched.formulas`` is the canonical low-priority textual kind
 to compare against now that ``graph_json`` is in ``_SKIP_KINDS``
 (and therefore deliberately absent from the priority map).
 """
    assert _DEFAULT_KIND_PRIORITY > _kind_priority("chunk")
    assert _DEFAULT_KIND_PRIORITY < _kind_priority("enriched.formulas")
    assert _kind_priority("totally_new_kind") == _DEFAULT_KIND_PRIORITY


def test_priority_table_has_canonical_textual_kinds():
    """Schema regression: the priority table must list the textual
 kinds the spec calls out (chunk, compiled.text,
 parsed_content_manifest). If one drops out by accident, the
 synthesizer would treat it as middle-priority and graph_json
 could win."""
    for required in ("chunk", "compiled.text", "parsed_content_manifest"):
        assert required in _KIND_PRIORITY
        assert _KIND_PRIORITY[required] < 10  # high-priority tier


# ---- End-to-end: priority drives evidence order ------------------


def test_chunk_appears_first_even_when_engine_scored_graph_higher(
    tmp_path: Path, ctx,
):
    """The headline behavior: if the engine returns
 `[graph_json (score=0.9), chunk (score=0.5)]`, the evidence
 builder must put the CHUNK first in the context — because
 textual evidence is what the synthesizer needs to ground a
 textual answer."""
    chunk_path = tmp_path / "chunk.ndjson"
    _write_chunk_ndjson(chunk_path, chunks=[{
        "chunk_id": "c-1",
        "body": "The proposal is due 20 May 2026.",
    }])
    chunk_record = _artifact(
        ctx=ctx, artifact_id="art-chunk", kind="chunk",
        location="chunks/art-chunk.ndjson",
    )
    graph_record = _artifact(
        ctx=ctx, artifact_id="art-graph", kind="graph_json",
        location="graph/art-graph.json",
    )
    registry = _StubRegistry([chunk_record, graph_record])

    def _resolver(record):
        # chunk artifacts go to the NDJSON file; graph never
        # actually opens — `_resolve_text_for_hit` for graph_json
        # uses `hit.preview`.
        if record.kind == "chunk":
            return chunk_path
        return tmp_path / "noop"

    # Engine scored graph_json higher — the policy must override.
    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            _hit(artifact_id="art-graph", kind="graph_json", score=0.9,
                 preview="graph hint"),
            _hit(artifact_id="art-chunk", kind="chunk",
                 chunk_id="c-1", score=0.5),
        ],
        artifact_registry=registry,
        path_resolver=_resolver,
    )
    # Chunk must come first in the evidence context.
    assert blocks[0].artifact_id == "art-chunk"
    assert "20 May 2026" in blocks[0].text


def test_multiple_textual_kinds_preserve_engine_score_order(
    tmp_path: Path, ctx,
):
    """Within the same priority tier (all chunks, scored
 differently), the engine's score order must be preserved.
 Python's stable sort guarantees this; the test pins the
 invariant so a future refactor to non-stable sort would fail
 loudly."""
    chunk_a_path = tmp_path / "chunk-a.ndjson"
    chunk_b_path = tmp_path / "chunk-b.ndjson"
    _write_chunk_ndjson(chunk_a_path, chunks=[
        {"chunk_id": "c-a", "body": "alpha body text"},
    ])
    _write_chunk_ndjson(chunk_b_path, chunks=[
        {"chunk_id": "c-b", "body": "bravo body text"},
    ])
    registry = _StubRegistry([
        _artifact(ctx=ctx, artifact_id="art-a", kind="chunk",
                  location="chunks/art-a.ndjson"),
        _artifact(ctx=ctx, artifact_id="art-b", kind="chunk",
                  location="chunks/art-b.ndjson"),
    ])
    files = {"art-a": chunk_a_path, "art-b": chunk_b_path}
    # Engine returns chunk-b first (higher score) then chunk-a.
    # Stable sort within the "chunk" tier must preserve this.
    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            _hit(artifact_id="art-b", kind="chunk", chunk_id="c-b", score=0.9),
            _hit(artifact_id="art-a", kind="chunk", chunk_id="c-a", score=0.4),
        ],
        artifact_registry=registry,
        path_resolver=lambda r: files[r.artifact_id],
    )
    assert [b.artifact_id for b in blocks] == ["art-b", "art-a"]


def test_graph_json_only_appears_if_budget_remains(
    tmp_path: Path, ctx,
):
    """When the budget caps out filling textual evidence, graph_json
 gets dropped — never crowds out the chunk that came first.
 Locks the spec's "graph never dominates" rule under tight
 budgets."""
    chunk_path = tmp_path / "chunk.ndjson"
    big_body = "x" * 1400  # ~ entire per-block cap
    _write_chunk_ndjson(chunk_path, chunks=[
        {"chunk_id": "c-1", "body": big_body},
    ])
    registry = _StubRegistry([
        _artifact(ctx=ctx, artifact_id="art-chunk", kind="chunk",
                  location="chunks/art-chunk.ndjson"),
        _artifact(ctx=ctx, artifact_id="art-graph", kind="graph_json",
                  location="graph/art-graph.json"),
    ])
    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            _hit(artifact_id="art-graph", kind="graph_json", score=0.9,
                 preview="graph metadata"),
            _hit(artifact_id="art-chunk", kind="chunk",
                 chunk_id="c-1", score=0.5),
        ],
        artifact_registry=registry,
        path_resolver=lambda r: (
            chunk_path if r.kind == "chunk" else tmp_path / "noop"
        ),
        max_blocks=1,  # tight cap: only one block fits
    )
    # The single emitted block is the textual chunk, never the
    # higher-scoring graph_json.
    assert len(blocks) == 1
    assert blocks[0].artifact_id == "art-chunk"


def test_skip_kinds_still_excluded_entirely(tmp_path: Path, ctx):
    """enriched.tables / .visuals / .document_map remain in
 _SKIP_KINDS — they're pure metadata, no usable text body
 (their `hit.preview` is just the artifact title). The
 priority policy applies to NON-SKIPPED kinds; truly
 unusable kinds still drop out wholesale."""
    chunk_path = tmp_path / "chunk.ndjson"
    _write_chunk_ndjson(chunk_path, chunks=[
        {"chunk_id": "c-1", "body": "text"},
    ])
    registry = _StubRegistry([
        _artifact(ctx=ctx, artifact_id="art-chunk", kind="chunk",
                  location="chunks/art-chunk.ndjson"),
        _artifact(ctx=ctx, artifact_id="art-tables",
                  kind="enriched.tables",
                  location="enriched/art-tables.json"),
    ])
    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            _hit(artifact_id="art-tables", kind="enriched.tables",
                 score=0.9, preview="enriched.tables/art-tables"),
            _hit(artifact_id="art-chunk", kind="chunk",
                 chunk_id="c-1", score=0.5),
        ],
        artifact_registry=registry,
        path_resolver=lambda r: chunk_path,
    )
    # Only the chunk survives — enriched.tables is in _SKIP_KINDS.
    assert [b.artifact_id for b in blocks] == ["art-chunk"]


# ---- Debug surfaces -----------------------------------------------


def test_debug_dict_surfaces_skipped_and_deprioritized_kinds():
    """When ``graph_json`` is in the retrieval set but doesn't reach
 the evidence context, the operator sees it in
 ``debug.skipped_kinds`` (because ``graph_json`` is now in
 ``_SKIP_KINDS`` — graph QA goes through RAGAnything.aquery,
 not the local textual synthesizer).

 ``enriched.formulas`` exercises the deprioritized bucket — same
 mechanism but for kinds with a low-priority slot rather than a
 skip rule.
 """
    from j1.validation.dtos import EvidenceBlockDTO
    from j1.validation.service import _build_manual_query_debug

    debug = _build_manual_query_debug(
        retrieved=[
            _hit(artifact_id="art-graph", kind="graph_json", score=0.9),
            _hit(artifact_id="art-formula", kind="enriched.formulas",
                 score=0.8),
            _hit(artifact_id="art-chunk", kind="chunk",
                 chunk_id="c-1", score=0.5),
        ],
        evidence_blocks=[EvidenceBlockDTO(
            artifact_id="art-chunk", artifact_type="chunk",
            text="some body",
        )],
        synthesized_answer="some answer",
        llm_trace=None,
    )
    # graph_json → skipped outright by _SKIP_KINDS.
    assert "graph_json" in debug["skipped_kinds"]
    assert "graph_json" not in debug["deprioritized_kinds"]
    # enriched.formulas → deprioritized but not skipped.
    assert "enriched.formulas" in debug["deprioritized_kinds"]
    # chunk made it into evidence — neither skipped nor deprioritized.
    assert "chunk" not in debug["deprioritized_kinds"]
    assert "chunk" not in debug["skipped_kinds"]


def test_debug_dict_omits_kinds_that_survived_the_filter():
    """A kind appearing in the after-filter set is NOT deprioritized
 — even if its priority value is high, it made it through."""
    from j1.validation.dtos import EvidenceBlockDTO
    from j1.validation.service import _build_manual_query_debug

    debug = _build_manual_query_debug(
        retrieved=[
            _hit(artifact_id="art-graph", kind="graph_json", score=0.9,
                 preview="graph"),
        ],
        # Suppose graph_json somehow made it through (e.g. tight
        # textual evidence ran out and graph_json took the slot).
        evidence_blocks=[EvidenceBlockDTO(
            artifact_id="art-graph", artifact_type="graph_json",
            text="graph",
        )],
        synthesized_answer="x",
        llm_trace=None,
    )
    # graph_json IS in artifact_types_after_filter → NOT counted
    # as deprioritized.
    assert "graph_json" not in debug["deprioritized_kinds"]


# ---- Regression guard ---------------------------------------------


def test_policy_does_not_regress_to_pre_phase_e_behavior(
    tmp_path: Path, ctx,
):
    """End-to-end regression guard: a typical mixed retrieval
 result (high-scoring graph_json + lower-scoring chunk) must
 land textual evidence first. If a future refactor accidentally
 disables the priority sort, this test catches it."""
    chunk_path = tmp_path / "chunk.ndjson"
    _write_chunk_ndjson(chunk_path, chunks=[
        {"chunk_id": "c-1", "body": "the answer is in the body"},
    ])
    registry = _StubRegistry([
        _artifact(ctx=ctx, artifact_id="art-chunk", kind="chunk",
                  location="chunks/art-chunk.ndjson"),
        _artifact(ctx=ctx, artifact_id="art-graph", kind="graph_json",
                  location="graph/art-graph.json"),
        _artifact(ctx=ctx, artifact_id="art-formula",
                  kind="enriched.formulas",
                  location="enriched/art-formula.json"),
    ])
    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            _hit(artifact_id="art-graph", kind="graph_json", score=0.99,
                 preview="graph hint"),
            _hit(artifact_id="art-formula", kind="enriched.formulas",
                 score=0.98, preview="formula hint"),
            _hit(artifact_id="art-chunk", kind="chunk",
                 chunk_id="c-1", score=0.10),
        ],
        artifact_registry=registry,
        path_resolver=lambda r: chunk_path if r.kind == "chunk" else tmp_path / "noop",
    )
    # The chunk WINS the first slot even though it had the
    # lowest engine score.
    assert blocks[0].artifact_id == "art-chunk"
    assert "the answer is in the body" in blocks[0].text
