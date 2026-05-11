"""Unit tests for ChunkProjector — no service, no REST.

Exercises every input shape (single-chunk JSON, multi-chunk JSON,
chunks-array-under-key, NDJSON), preview truncation, snake_case +
camelCase tolerance, and malformed-entry resilience.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.ingestion_review.projectors import ChunkProjector
from j1.ingestion_review.projectors.chunks import PREVIEW_MAX_CHARS
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def _record(
    artifact_id: str,
    location: str,
    *,
    kind: str = "chunk",
    source_document_ids: list[str] | None = None,
) -> ArtifactRecord:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ProjectContext(tenant_id="acme", project_id="alpha"),
        kind=kind,
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=source_document_ids or [],
    )


def _resolver_for(mapping: dict[str, Path]):
    """Build a path-resolver closure mapping artifact_id → file path."""
    def _resolve(record: ArtifactRecord) -> Path:
        return mapping[record.artifact_id]
    return _resolve


# ---- Single-chunk JSON ----------------------------------------------


def test_projector_reads_single_chunk_json_object(tmp_path):
    path = tmp_path / "single.json"
    path.write_text(json.dumps({
        "chunkId": "ch-1",
        "body": "Hello, world.",
        "pageStart": 1,
        "pageEnd": 1,
        "tokenCount": 4,
        "confidence": 0.92,
        "section": "Intro",
    }), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/single.json")])

    assert len(records) == 1
    chunk = records[0]
    assert chunk.chunk_id == "ch-1"
    assert chunk.body == "Hello, world."
    assert chunk.page_start == 1
    assert chunk.token_count == 4
    assert chunk.confidence == 0.92


def test_projector_accepts_snake_case_field_names(tmp_path):
    path = tmp_path / "snake.json"
    path.write_text(json.dumps({
        "chunk_id": "ch-2",
        "body": "Body text.",
        "page_start": 5,
        "token_count": 7,
    }), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/snake.json")])

    assert records[0].chunk_id == "ch-2"
    assert records[0].page_start == 5
    assert records[0].token_count == 7


def test_projector_falls_back_to_synthetic_chunk_id(tmp_path):
    """Producer didn't set a chunk_id → synthesised from artifact_id +
 index. Keeps the chunk addressable via the detail endpoint."""
    path = tmp_path / "anon.json"
    path.write_text(json.dumps({"body": "no id"}), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/anon.json")])

    assert records[0].chunk_id == "a1#0"


def test_projector_accepts_content_field_alias(tmp_path):
    path = tmp_path / "content.json"
    path.write_text(json.dumps({
        "chunk_id": "ch", "content": "alt body field",
    }), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/content.json")])

    assert records[0].body == "alt body field"


def test_projector_drops_chunk_with_no_body(tmp_path):
    """A chunk with no text isn't reviewable — drop it rather than
 surface a blank row to the FE."""
    path = tmp_path / "empty.json"
    path.write_text(json.dumps([
        {"chunk_id": "ok", "body": "fine"},
        {"chunk_id": "bad"},  # no body
        {"chunk_id": "blank", "body": "   "},  # whitespace-only
    ]), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/empty.json")])

    assert [r.chunk_id for r in records] == ["ok"]


# ---- Multi-chunk JSON -----------------------------------------------


def test_projector_reads_top_level_array_of_chunks(tmp_path):
    path = tmp_path / "many.json"
    path.write_text(json.dumps([
        {"chunk_id": "ch-1", "body": "first"},
        {"chunk_id": "ch-2", "body": "second"},
        {"chunk_id": "ch-3", "body": "third"},
    ]), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/many.json")])

    assert [r.chunk_id for r in records] == ["ch-1", "ch-2", "ch-3"]


def test_projector_reads_chunks_under_chunks_key(tmp_path):
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({
        "version": 1,
        "chunks": [
            {"chunk_id": "ch-1", "body": "a"},
            {"chunk_id": "ch-2", "body": "b"},
        ],
    }), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/wrapped.json")])

    assert [r.chunk_id for r in records] == ["ch-1", "ch-2"]


# ---- NDJSON ---------------------------------------------------------


def test_projector_reads_ndjson(tmp_path):
    path = tmp_path / "stream.ndjson"
    path.write_text(
        "\n".join([
            json.dumps({"chunk_id": "ch-1", "body": "line one"}),
            "",  # blank line — must be tolerated
            json.dumps({"chunk_id": "ch-2", "body": "line two"}),
        ]),
        encoding="utf-8",
    )

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/stream.ndjson")])

    assert [r.chunk_id for r in records] == ["ch-1", "ch-2"]


def test_projector_skips_malformed_ndjson_line(tmp_path):
    path = tmp_path / "broken.ndjson"
    path.write_text(
        "\n".join([
            json.dumps({"chunk_id": "ok", "body": "fine"}),
            "{not valid json",
            json.dumps({"chunk_id": "ok2", "body": "also fine"}),
        ]),
        encoding="utf-8",
    )

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/broken.ndjson")])

    assert [r.chunk_id for r in records] == ["ok", "ok2"]


def test_projector_jsonl_extension_treated_as_ndjson(tmp_path):
    path = tmp_path / "stream.jsonl"
    path.write_text(
        json.dumps({"chunk_id": "ch", "body": "x"}) + "\n",
        encoding="utf-8",
    )

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/stream.jsonl")])

    assert len(records) == 1


# ---- Linked assets --------------------------------------------------


def test_projector_normalises_linked_assets(tmp_path):
    path = tmp_path / "linked.json"
    path.write_text(json.dumps({
        "chunk_id": "ch", "body": "hi",
        "linked_assets": [
            {"artifact_id": "img-1", "kind": "enriched.visuals"},
            {"artifactId": "tab-1", "kind": "enriched.tables"},  # camelCase
            {"kind": "missing-id"},  # dropped — no artifact_id
            "not a dict",  # dropped
        ],
    }), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/linked.json")])

    assets = records[0].linked_assets
    assert [a.artifact_id for a in assets] == ["img-1", "tab-1"]
    assert assets[0].kind == "enriched.visuals"


# ---- Source-artifact lineage ----------------------------------------


def test_projector_falls_back_to_artifact_id_for_source(tmp_path):
    """When the chunk dict didn't pin a source_artifact_id, the chunk's
 own producing artifact IS the source — surface that so the FE
 can always link to a content endpoint."""
    path = tmp_path / "no-src.json"
    path.write_text(json.dumps({"chunk_id": "ch", "body": "hi"}), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/no-src.json")])

    assert records[0].source_artifact_id == "a1"


def test_projector_respects_explicit_source_artifact_id(tmp_path):
    path = tmp_path / "with-src.json"
    path.write_text(json.dumps({
        "chunk_id": "ch", "body": "hi",
        "sourceArtifactId": "compile-output-7",
    }), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/with-src.json")])

    assert records[0].source_artifact_id == "compile-output-7"


# ---- Resilience -----------------------------------------------------


def test_projector_skips_artifact_when_path_resolver_raises(tmp_path):
    def _bad_resolver(_record):
        raise RuntimeError("blocked")
    projector = ChunkProjector(path_resolver=_bad_resolver)
    records = projector.project_records([_record("a1", "compiled/x.json")])
    assert records == []


def test_projector_skips_missing_file(tmp_path):
    projector = ChunkProjector(
        path_resolver=_resolver_for({"a1": tmp_path / "ghost.json"}),
    )
    records = projector.project_records([_record("a1", "compiled/ghost.json")])
    assert records == []


def test_projector_skips_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json at all", encoding="utf-8")
    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([_record("a1", "compiled/bad.json")])
    assert records == []


def test_projector_ignores_non_chunk_artifacts(tmp_path):
    path = tmp_path / "skip.json"
    path.write_text(json.dumps({"chunk_id": "ch", "body": "x"}), encoding="utf-8")
    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))

    records = projector.project_records([
        _record("a1", "compiled/skip.json", kind="enriched.tables"),
    ])

    assert records == []


# ---- Preview / detail DTOs ------------------------------------------


def test_preview_truncates_and_squashes_whitespace():
    from j1.ingestion_review.projectors.chunks import _ChunkRecord, _make_preview

    long_text = "x" * (PREVIEW_MAX_CHARS + 100)
    preview = _make_preview(long_text)
    assert len(preview) == PREVIEW_MAX_CHARS
    assert preview.endswith("…")

    multi = "  hello\n\n\tworld   "
    assert _make_preview(multi) == "hello world"


def test_to_preview_and_detail_round_trip(tmp_path):
    path = tmp_path / "rt.json"
    path.write_text(json.dumps({
        "chunk_id": "ch", "body": "hello",
        "tokenCount": 3, "confidence": 0.5,
    }), encoding="utf-8")

    projector = ChunkProjector(path_resolver=_resolver_for({"a1": path}))
    records = projector.project_records([
        _record("a1", "compiled/rt.json", source_document_ids=["doc-A"]),
    ])

    preview = projector.to_preview(records[0])
    assert preview.preview == "hello"
    assert preview.token_count == 3
    assert preview.confidence == 0.5
    assert preview.source_artifact_id == "a1"

    detail = projector.to_detail(records[0], lineage={"stage": "compile"})
    assert detail.body == "hello"
    assert detail.lineage == {"stage": "compile"}
