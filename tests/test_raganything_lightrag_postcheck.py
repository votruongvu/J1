"""Tests for `_detect_lightrag_doc_failure` — the post-compile check
that surfaces silent LightRAG failures (e.g. embedding dimension
mismatch) as compile failures.

Without the post-check, RAGAnything's `process_document_complete`
swallows the underlying error: LightRAG logs the traceback as ERROR,
records `status=failed` in `kv_store_doc_status.json`, and returns
normally. The bridge would then report compile=succeeded for a
document that produced zero chunks, leaving the operator with a
disabled Chunks tab and no actionable error message.

The post-check reads `kv_store_doc_status.json`, looks up the just-
processed document, and returns the LightRAG-supplied `error_msg`
when the entry's status is `failed`. `default_compile` translates a
non-None return into an `ArtifactProcessingResult(status=FAILED)`,
which trips the workflow's required-step contract and surfaces the
real cause to the operator.
"""

from __future__ import annotations

import json
from pathlib import Path


from j1.providers.raganything._bridge import _detect_lightrag_doc_failure


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_returns_error_msg_for_failed_doc(tmp_path):
    """The canonical case: LightRAG marked the doc as failed and put
 a useful error message in `error_msg`. The post-check returns
 that string verbatim so the operator sees the real cause."""
    _write_status(tmp_path / "kv_store_doc_status.json", {
        "doc-1": {
            "status": "failed",
            "error_msg": (
                "Embedding dimension mismatch detected: "
                "total elements (1536) cannot be evenly divided by "
                "expected dimension (1024)."
            ),
            "chunks_count": 2,
        },
    })

    result = _detect_lightrag_doc_failure(tmp_path, document_id="doc-1")

    assert result is not None
    assert "dimension mismatch" in result
    assert "1536" in result
    assert "1024" in result


def test_returns_none_for_processed_doc(tmp_path):
    """Successful runs leave `status=processed`; the post-check must
 return None so the bridge proceeds to draft collection."""
    _write_status(tmp_path / "kv_store_doc_status.json", {
        "doc-1": {"status": "processed", "chunks_count": 5},
    })
    assert _detect_lightrag_doc_failure(tmp_path, document_id="doc-1") is None


def test_returns_none_when_doc_missing_from_status(tmp_path):
    """Different document id in the file (e.g. stale state from a
 prior run) — must not falsely fail the current compile."""
    _write_status(tmp_path / "kv_store_doc_status.json", {
        "doc-other": {"status": "failed", "error_msg": "stale failure"},
    })
    assert _detect_lightrag_doc_failure(tmp_path, document_id="doc-1") is None


def test_returns_none_when_status_file_absent(tmp_path):
    """Empty storage dir (e.g. a fresh deployment that hasn't written
 anything yet) — return None so the bridge falls through to its
 usual success path."""
    assert _detect_lightrag_doc_failure(tmp_path, document_id="doc-1") is None


def test_returns_none_when_storage_dir_missing(tmp_path):
    """Storage dir doesn't exist on disk at all — no-op."""
    missing = tmp_path / "does-not-exist"
    assert _detect_lightrag_doc_failure(missing, document_id="doc-1") is None


def test_handles_invalid_json(tmp_path):
    """Mid-write / truncated KV file mustn't crash compile — caller
 treats `None` as 'no signal, proceed normally'."""
    bad = tmp_path / "kv_store_doc_status.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not valid json", encoding="utf-8")
    assert _detect_lightrag_doc_failure(tmp_path, document_id="doc-1") is None


def test_handles_non_dict_top_level(tmp_path):
    """Defensive: future LightRAG drift to a different top-level shape
 must not raise."""
    bad = tmp_path / "kv_store_doc_status.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("[]", encoding="utf-8")
    assert _detect_lightrag_doc_failure(tmp_path, document_id="doc-1") is None


def test_failed_without_error_msg_returns_default_message(tmp_path):
    """LightRAG sometimes records `status=failed` without populating
 `error_msg`. We still surface a non-None string so the workflow's
 required-step contract trips — better a generic message than a
 silent success."""
    _write_status(tmp_path / "kv_store_doc_status.json", {
        "doc-1": {"status": "failed"},
    })
    result = _detect_lightrag_doc_failure(tmp_path, document_id="doc-1")
    assert result is not None
    assert "failed" in result.lower()


def test_finds_status_file_in_nested_storage_subdir(tmp_path):
    """Forward-compat with the old `<workdir>/storage/...` layout —
 `rglob` finds the file at any depth, matching the existing
 `_chunk_drafts_from_storage` behaviour."""
    _write_status(tmp_path / "storage" / "kv_store_doc_status.json", {
        "doc-1": {"status": "failed", "error_msg": "nested layout failure"},
    })
    result = _detect_lightrag_doc_failure(tmp_path, document_id="doc-1")
    assert result == "nested layout failure"


def test_status_match_is_case_insensitive(tmp_path):
    """LightRAG variants have used both `failed` and `FAILED` over
 time — accept either spelling so the post-check stays stable
 across vendor upgrades."""
    _write_status(tmp_path / "kv_store_doc_status.json", {
        "doc-1": {"status": "FAILED", "error_msg": "uppercase status"},
    })
    assert _detect_lightrag_doc_failure(tmp_path, document_id="doc-1") == "uppercase status"
