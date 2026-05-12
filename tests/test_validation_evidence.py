"""Unit tests for `j1.validation.evidence.build_evidence_blocks`.

These tests cover the evidence-building rules that sit between the
retrieval engine's metadata-only projection and the LLM synthesizer:

 * Chunk-kind artifacts: load real body via ChunkProjector.
 * compiled.text fallback: leading window read from disk.
 * enriched.* / graph_json: skipped entirely.
 * Deduplication: compiled.text overlapping a prior chunk is dropped.
 * Budget: cumulative-char cap stops adding once exceeded.
 * Unknown kinds: degrade to the hit's preview rather than crash.
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
from j1.validation.evidence import build_evidence_blocks


_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


class _StubRegistry:
    """Tiny in-memory registry sufficient for evidence-builder tests.

 The real `ArtifactRegistry` is heavier (jsonl persistence, project-
 scoped reads). Tests only need `get(ctx, artifact_id)`."""

    def __init__(self, records: list[ArtifactRecord]):
        self._by_id = {r.artifact_id: r for r in records}

    def get(self, ctx, artifact_id):  # noqa: ARG002 — proto signature
        if artifact_id not in self._by_id:
            raise ArtifactNotFoundError(artifact_id)
        return self._by_id[artifact_id]

    def list_artifacts(self, ctx, *, kind=None):  # noqa: ARG002
        if kind is None:
            return list(self._by_id.values())
        return [r for r in self._by_id.values() if r.kind == kind]


def _artifact(
    *,
    artifact_id: str,
    kind: str,
    location: str,
    ctx: ProjectContext,
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=100,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _write_chunk_ndjson(
    path: Path, *, chunks: list[dict],
) -> None:
    """Write a list of chunk dicts as NDJSON at the given path.
 Matches the format the production chunk projector reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c) + "\n")


def _hit(
    *,
    artifact_id: str,
    artifact_kind: str,
    chunk_id: str | None = None,
    preview: str = "",
    score: float = 0.5,
) -> RetrievedChunkRefDTO:
    return RetrievedChunkRefDTO(
        artifact_id=artifact_id,
        chunk_id=chunk_id,
        run_id="run-1",
        document_id="doc-1",
        source_location=None,
        score=score,
        preview=preview,
        artifact_kind=artifact_kind,
    )


# ---- Chunk-kind happy path ----------------------------------------


def test_chunk_artifact_loads_real_body(tmp_path: Path, ctx):
    """A `kind=chunk` hit should resolve to the chunk's actual body
 text — not the artifact title. This is the central bug the
 evidence builder fixes."""
    artifact = _artifact(
        artifact_id="art-1",
        kind="chunk",
        location=f"{tmp_path.name}/chunks/art-1.ndjson",
        ctx=ctx,
    )
    chunk_path = tmp_path / "chunks" / "art-1.ndjson"
    _write_chunk_ndjson(chunk_path, chunks=[{
        "chunk_id": "c1",
        "body": "Assessment must stay lightweight; a full MinerU parse is expensive.",
        "page_start": 1,
        "page_end": 1,
        "section": "Workflow",
    }])
    registry = _StubRegistry([artifact])

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[_hit(
            artifact_id="art-1",
            artifact_kind="chunk",
            chunk_id="c1",
            preview="chunk/art-1",  # the engine's metadata preview (title-ish)
        )],
        artifact_registry=registry,
        path_resolver=lambda r: chunk_path,
    )

    assert len(blocks) == 1
    assert "lightweight" in blocks[0].text
    assert "MinerU" in blocks[0].text
    assert blocks[0].chunk_id == "c1"
    assert blocks[0].page_start == 1
    assert blocks[0].page_end == 1
    assert blocks[0].section == "Workflow"
    assert blocks[0].artifact_type == "chunk"


def test_chunk_artifact_with_unknown_chunk_id_falls_back_to_first(
    tmp_path: Path, ctx,
):
    """If the hit's chunk_id doesn't match any chunk in the artifact
 (legacy hit, replayed run, etc.) we fall back to the first
 chunk in the file rather than dropping the hit entirely."""
    artifact = _artifact(
        artifact_id="art-1", kind="chunk",
        location=f"chunks/art-1.ndjson", ctx=ctx,
    )
    chunk_path = tmp_path / "art-1.ndjson"
    _write_chunk_ndjson(chunk_path, chunks=[
        {"chunk_id": "c-real", "body": "first chunk body", "page_start": 1},
        {"chunk_id": "c-other", "body": "second chunk body", "page_start": 2},
    ])
    registry = _StubRegistry([artifact])

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[_hit(
            artifact_id="art-1", artifact_kind="chunk",
            chunk_id="c-nonexistent",
        )],
        artifact_registry=registry,
        path_resolver=lambda r: chunk_path,
    )

    assert len(blocks) == 1
    assert blocks[0].text == "first chunk body"


# ---- compiled.text fallback ---------------------------------------


def test_compiled_text_artifact_reads_leading_window(tmp_path: Path, ctx):
    """`kind=compiled.text` is the whole-document compile output;
 we read a leading window so the LLM gets useful overview text
 without burning the entire context window."""
    artifact = _artifact(
        artifact_id="ct-1", kind="compiled.text",
        location="compiled/ct-1.txt", ctx=ctx,
    )
    text_path = tmp_path / "ct-1.txt"
    text_path.write_text(
        "The J1 workflow targets six stages.\n"
        "Assessment must stay lightweight; a full MinerU parse belongs "
        "to compile.\n" + ("...padding..." * 200),
        encoding="utf-8",
    )
    registry = _StubRegistry([artifact])

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[_hit(
            artifact_id="ct-1", artifact_kind="compiled.text",
        )],
        artifact_registry=registry,
        path_resolver=lambda r: text_path,
    )

    assert len(blocks) == 1
    assert "Assessment must stay lightweight" in blocks[0].text
    assert blocks[0].artifact_type == "compiled.text"
    # Page info isn't recorded for compiled.text — it's a document-
    # level artifact, not chunk-grained.
    assert blocks[0].page_start is None


# ---- skip / dedup -------------------------------------------------


def test_enriched_document_map_is_skipped(tmp_path: Path, ctx):
    """`enriched.document_map` is structured JSON metadata, not prose;
 dumping it into the prompt confuses small local LLMs. Skipped."""
    artifact = _artifact(
        artifact_id="em-1", kind="enriched.document_map",
        location="enriched/em-1.json", ctx=ctx,
    )
    registry = _StubRegistry([artifact])

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[_hit(
            artifact_id="em-1", artifact_kind="enriched.document_map",
            preview="enriched.document_map/em-1",
        )],
        artifact_registry=registry,
        path_resolver=lambda r: tmp_path / "noop",
    )

    assert blocks == []


def test_compiled_text_overlap_with_prior_chunk_is_deduplicated(
    tmp_path: Path, ctx,
):
    """If a chunk's body and a compiled.text window start with the same
 200 chars, we treat them as duplicates and drop the second.
 Prevents token waste on near-identical evidence."""
    chunk_artifact = _artifact(
        artifact_id="art-1", kind="chunk",
        location="chunks/art-1.ndjson", ctx=ctx,
    )
    chunk_path = tmp_path / "art-1.ndjson"
    long_body = "Assessment is the deterministic profiling step that " * 20
    _write_chunk_ndjson(chunk_path, chunks=[{
        "chunk_id": "c1", "body": long_body, "page_start": 1,
    }])

    text_artifact = _artifact(
        artifact_id="ct-1", kind="compiled.text",
        location="compiled/ct-1.txt", ctx=ctx,
    )
    text_path = tmp_path / "ct-1.txt"
    text_path.write_text(long_body, encoding="utf-8")

    registry = _StubRegistry([chunk_artifact, text_artifact])

    def _resolver(record):
        return chunk_path if record.kind == "chunk" else text_path

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            _hit(artifact_id="art-1", artifact_kind="chunk", chunk_id="c1"),
            _hit(artifact_id="ct-1", artifact_kind="compiled.text"),
        ],
        artifact_registry=registry,
        path_resolver=_resolver,
    )

    assert len(blocks) == 1
    assert blocks[0].artifact_id == "art-1"


# ---- budget -------------------------------------------------------


def test_total_budget_caps_cumulative_chars(tmp_path: Path, ctx):
    """The cumulative-char budget stops the builder from emitting
 more blocks once exceeded, even if max_blocks would allow them.
 Mirrors the synthesizer's prompt cap so the FE's "Evidence Sent
 to LLM" reflects what the model actually saw."""
    artifacts = []
    chunk_files = []
    for i in range(5):
        a = _artifact(
            artifact_id=f"art-{i}", kind="chunk",
            location=f"chunks/art-{i}.ndjson", ctx=ctx,
        )
        artifacts.append(a)
        path = tmp_path / f"art-{i}.ndjson"
        # 500 chars per chunk; budget of 1000 = at most 2 blocks
        _write_chunk_ndjson(path, chunks=[{
            "chunk_id": f"c{i}", "body": f"chunk-{i}: " + "x" * 490,
        }])
        chunk_files.append(path)

    registry = _StubRegistry(artifacts)
    files_by_id = {a.artifact_id: chunk_files[i] for i, a in enumerate(artifacts)}

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            _hit(artifact_id=f"art-{i}", artifact_kind="chunk", chunk_id=f"c{i}")
            for i in range(5)
        ],
        artifact_registry=registry,
        path_resolver=lambda r: files_by_id[r.artifact_id],
        total_budget_chars=1000,
    )

    assert len(blocks) <= 3
    total = sum(len(b.text) for b in blocks)
    assert total <= 1000 + 100  # +slack for the in-loop truncation


def test_max_blocks_caps_count(tmp_path: Path, ctx):
    """Independent of char budget, `max_blocks` caps the number of
 evidence blocks so a flood of tiny chunks can't crowd out
 well-placed long ones."""
    artifacts = [
        _artifact(
            artifact_id=f"art-{i}", kind="chunk",
            location=f"chunks/art-{i}.ndjson", ctx=ctx,
        )
        for i in range(10)
    ]
    chunk_files = []
    for i, _ in enumerate(artifacts):
        path = tmp_path / f"art-{i}.ndjson"
        _write_chunk_ndjson(path, chunks=[{
            "chunk_id": f"c{i}", "body": f"unique chunk text number {i}",
        }])
        chunk_files.append(path)
    registry = _StubRegistry(artifacts)
    files_by_id = {a.artifact_id: chunk_files[i] for i, a in enumerate(artifacts)}

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            _hit(artifact_id=f"art-{i}", artifact_kind="chunk", chunk_id=f"c{i}")
            for i in range(10)
        ],
        artifact_registry=registry,
        path_resolver=lambda r: files_by_id[r.artifact_id],
        max_blocks=3,
    )

    assert len(blocks) == 3


# ---- unknown kinds + missing artifacts ----------------------------


def test_unknown_kind_falls_back_to_hit_preview(ctx, tmp_path: Path):
    """A hit whose kind we don't explicitly handle (e.g. a future
 artifact type) shouldn't crash — we degrade gracefully by
 surfacing the preview the engine gave us."""
    artifact = _artifact(
        artifact_id="x-1", kind="unknown_future_kind",
        location="x/x-1", ctx=ctx,
    )
    registry = _StubRegistry([artifact])

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[_hit(
            artifact_id="x-1",
            artifact_kind="unknown_future_kind",
            preview="some title hint from the engine",
        )],
        artifact_registry=registry,
        path_resolver=lambda r: tmp_path / "noop",
    )

    assert len(blocks) == 1
    assert "title hint" in blocks[0].text


def test_missing_artifact_is_silently_skipped(ctx, tmp_path: Path):
    """Stale retrieval hit (artifact deleted between index + query)
 shouldn't break the response. We log + skip and move on."""
    registry = _StubRegistry([])

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[_hit(
            artifact_id="ghost-1",
            artifact_kind="chunk",
            chunk_id="cX",
        )],
        artifact_registry=registry,
        path_resolver=lambda r: tmp_path / "noop",
    )

    assert blocks == []
