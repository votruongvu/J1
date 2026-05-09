"""Tests for the LightRAG `kv_store_text_chunks.json` → canonical
`kind="chunk"` extraction in the RAGAnything bridge.

The Results > Chunks tab only populates when artifacts of
`kind="chunk"` exist. Until this fix, no producer in the dev stack
emitted them — LightRAG persisted per-chunk text inside its storage
dir but the bridge surfaced the file as a `graph_json` artifact
(lumped with entities/relations), so the chunk projector never saw
chunk-shaped data.

The new `_chunk_drafts_from_storage` reads
`kv_store_text_chunks.json`, projects each entry into the canonical
chunk JSON shape (`{chunkId, body, tokenCount, metadata}`), and emits
one `ArtifactDraft(kind="chunk")` per chunk. The `_graph_drafts_from_storage`
helper now excludes that filename so chunks aren't double-classified.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j1.providers.raganything._bridge import (
    _chunk_drafts_from_storage,
    _graph_drafts_from_storage,
)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_emits_one_chunk_draft_per_entry(tmp_path):
    """LightRAG's KV store is a top-level dict keyed by chunk id —
    every entry becomes one canonical chunk artifact draft."""
    _write(tmp_path / "kv_store_text_chunks.json", {
        "chunk-001": {
            "tokens": 96,
            "content": "First section talks about cloud growth.",
            "full_doc_id": "doc-q4",
            "chunk_order_index": 0,
            "file_path": "doc-q4.pdf",
        },
        "chunk-002": {
            "tokens": 134,
            "content": "Second section covers margins.",
            "full_doc_id": "doc-q4",
            "chunk_order_index": 1,
            "file_path": "doc-q4.pdf",
        },
    })

    drafts = _chunk_drafts_from_storage(tmp_path, document_id="doc-q4")

    assert len(drafts) == 2
    for draft in drafts:
        assert draft.kind == "chunk"
        assert draft.suggested_extension == ".json"
        assert draft.source_document_ids == ["doc-q4"]
    # Each draft's payload carries the canonical chunk shape the FE
    # ChunkProjector reads.
    payloads = [json.loads(d.content) for d in drafts]
    by_id = {p["chunkId"]: p for p in payloads}
    assert by_id["chunk-001"]["body"] == "First section talks about cloud growth."
    assert by_id["chunk-001"]["tokenCount"] == 96
    assert by_id["chunk-001"]["metadata"]["chunkOrderIndex"] == 0
    assert by_id["chunk-001"]["metadata"]["fullDocId"] == "doc-q4"


def test_skips_empty_content_entries(tmp_path):
    """LightRAG sometimes emits placeholder chunks with empty
    content for split boundaries. Surfacing them as chunks would
    show blank rows on the FE — drop silently."""
    _write(tmp_path / "kv_store_text_chunks.json", {
        "ok": {"tokens": 5, "content": "real content"},
        "empty": {"tokens": 0, "content": ""},
        "whitespace": {"tokens": 0, "content": "   \n  "},
    })

    drafts = _chunk_drafts_from_storage(tmp_path, document_id="doc-1")

    assert len(drafts) == 1
    assert json.loads(drafts[0].content)["chunkId"] == "ok"


def test_returns_empty_when_storage_missing(tmp_path):
    """No storage dir on disk yet (e.g. compile of an empty doc) —
    return empty drafts list rather than raising."""
    drafts = _chunk_drafts_from_storage(
        tmp_path / "does-not-exist", document_id="doc-1",
    )
    assert drafts == []


def test_returns_empty_when_chunks_file_absent(tmp_path):
    """Storage dir exists but no `kv_store_text_chunks.json` — the
    extractor must NOT explode (this happens when the LightRAG
    pipeline is partially populated)."""
    (tmp_path / "vdb_entities.json").write_text("{}", encoding="utf-8")
    drafts = _chunk_drafts_from_storage(tmp_path, document_id="doc-1")
    assert drafts == []


def test_handles_invalid_json(tmp_path):
    """A truncated / mid-write KV file shouldn't crash compile —
    return empty drafts so the workflow proceeds and the operator
    sees the empty Chunks tab rather than a hard failure."""
    bad = tmp_path / "kv_store_text_chunks.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not valid json", encoding="utf-8")

    drafts = _chunk_drafts_from_storage(tmp_path, document_id="doc-1")
    assert drafts == []


def test_tolerates_non_dict_top_level(tmp_path):
    """LightRAG variants always emit a top-level dict; defensive code
    path for any future drift."""
    bad = tmp_path / "kv_store_text_chunks.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("[]", encoding="utf-8")
    drafts = _chunk_drafts_from_storage(tmp_path, document_id="doc-1")
    assert drafts == []


def test_token_count_falls_back_to_none_when_missing(tmp_path):
    """Producer didn't supply `tokens` → tokenCount is null, NOT
    fabricated. The FE shows '—' rather than a misleading zero."""
    _write(tmp_path / "kv_store_text_chunks.json", {
        "ch": {"content": "body without tokens field"},
    })
    drafts = _chunk_drafts_from_storage(tmp_path, document_id="doc-1")
    assert json.loads(drafts[0].content)["tokenCount"] is None


def test_chunks_found_at_workdir_root(tmp_path):
    """Regression: LightRAG writes `kv_store_text_chunks.json`
    DIRECTLY into `working_dir`, not into a `<workdir>/storage`
    subdirectory. The settings default sets `storage_dir = workdir`,
    so `_chunk_drafts_from_storage(workdir, ...)` must find the file
    at the root level.

    Before the storage-default fix, the helper was called with
    `<workdir>/storage` which doesn't exist for any LightRAG run —
    the Chunks tab stayed disabled even after a successful index."""
    _write(tmp_path / "kv_store_text_chunks.json", {
        "chunk-001": {"tokens": 5, "content": "real chunk text"},
    })
    drafts = _chunk_drafts_from_storage(tmp_path, document_id="doc-1")
    assert len(drafts) == 1
    assert json.loads(drafts[0].content)["chunkId"] == "chunk-001"


def test_chunks_found_at_legacy_storage_subdir(tmp_path):
    """Forward-compat: deployments that explicitly set
    `J1_RAGANYTHING_STORAGE_DIR=<workdir>/storage` (the OLD default)
    must keep working. `rglob` already recurses, so the helper
    finds the file at any depth."""
    _write(tmp_path / "storage" / "kv_store_text_chunks.json", {
        "chunk-001": {"tokens": 5, "content": "legacy layout"},
    })
    drafts = _chunk_drafts_from_storage(tmp_path, document_id="doc-1")
    assert len(drafts) == 1
    assert "legacy layout" in json.loads(drafts[0].content)["body"]


def test_graph_drafts_excludes_chunks_file(tmp_path):
    """The `_graph_drafts_from_storage` helper used to surface
    `kv_store_text_chunks.json` as a `graph_json` artifact (it
    matched the `kv_store*.json` pattern). Now that chunks have
    their own kind, the graph extractor must skip that file —
    otherwise the FE sees the chunks twice (once under graph,
    once under chunks).

    Also excludes `kv_store_doc_status.json` — it's a per-document
    state machine + duplicate-detection record store, not graph
    data. Surfacing it under graph put `[DUPLICATE] Original
    document: ...` rows in the Knowledge Graph tab for runs that
    re-uploaded the same checksum."""
    _write(tmp_path / "kv_store_text_chunks.json", {"ch": {"content": "x"}})
    _write(tmp_path / "kv_store_doc_status.json", {
        "d0d59aaf": {"status": "processed", "content": ""},
        "dup-9deade": {
            "status": "failed",
            "content_summary": "[DUPLICATE] Original document: d0d59aaf",
        },
    })
    _write(tmp_path / "kv_store_full_docs.json", {"d": {"id": "d"}})
    _write(tmp_path / "vdb_entities.json", {"e1": {"__id__": "e1"}})

    graph_drafts = _graph_drafts_from_storage(
        tmp_path, artifact_ids=["compile-1"],
    )

    filenames = {
        d.metadata.get("filename") for d in graph_drafts if d.kind == "graph_json"
    }
    # Chunks + doc_status files are excluded; other KV files + entities
    # surface as graph_json.
    assert "kv_store_text_chunks.json" not in filenames
    assert "kv_store_doc_status.json" not in filenames
    assert "kv_store_full_docs.json" in filenames
    assert "vdb_entities.json" in filenames
