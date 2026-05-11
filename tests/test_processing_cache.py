"""Tests for `j1.processing.cache` — the processor-result cache.

The cache exists so Temporal activity retries don't re-run expensive
deterministic processors when a previous attempt already produced
the artifact. These tests pin the contract callers depend on:

 * `make_cache_key` is deterministic across (document_hash,
 processor_kind, processor_version, mode) — change ANY input,
 get a different key.
 * Lookup returns the latest snapshot per cache_key, so two
 racing activity attempts (failed → completed) leave the
 completed entry as the visible state.
 * The store survives re-instantiation (file-backed).
"""

from __future__ import annotations

from datetime import datetime, timezone

from j1.processing.cache import (
    CACHE_STATUS_COMPLETED,
    CACHE_STATUS_FAILED,
    CACHE_STATUS_PROCESSING,
    JsonlProcessingResultCache,
    ProcessingCacheEntry,
    make_cache_key,
)


def _entry(
    *,
    cache_key: str,
    status: str,
    artifact_ids: tuple[str, ...] = (),
    error_type: str | None = None,
    error_message: str | None = None,
    document_hash: str = "sha256:abc",
    processor_kind: str = "mock.compiler",
) -> ProcessingCacheEntry:
    now = datetime.now(timezone.utc)
    return ProcessingCacheEntry(
        cache_key=cache_key,
        document_id="doc-1",
        document_hash=document_hash,
        processor_kind=processor_kind,
        processor_version="",
        mode="",
        status=status,
        artifact_ids=artifact_ids,
        created_at=now,
        updated_at=now,
        error_type=error_type,
        error_message=error_message,
    )


def test_make_cache_key_is_deterministic_for_same_inputs():
    a = make_cache_key(
        document_hash="sha256:x", processor_kind="mock", processor_version="1",
    )
    b = make_cache_key(
        document_hash="sha256:x", processor_kind="mock", processor_version="1",
    )
    assert a == b
    assert len(a) == 64  # sha256 hex digest


def test_make_cache_key_changes_with_each_input():
    base = make_cache_key(document_hash="sha256:x", processor_kind="mock")
    diff_hash = make_cache_key(document_hash="sha256:y", processor_kind="mock")
    diff_kind = make_cache_key(document_hash="sha256:x", processor_kind="other")
    diff_ver = make_cache_key(
        document_hash="sha256:x", processor_kind="mock", processor_version="2",
    )
    diff_mode = make_cache_key(
        document_hash="sha256:x", processor_kind="mock", mode="vlm",
    )
    assert len({base, diff_hash, diff_kind, diff_ver, diff_mode}) == 5


def test_lookup_returns_latest_snapshot_per_key(workspace, ctx):
    """Two activity attempts: first fails, second succeeds. Latest-
 snapshot semantics mean the visible state is `completed`."""
    cache = JsonlProcessingResultCache(workspace)
    key = make_cache_key(document_hash="sha256:doc", processor_kind="mock")

    cache.upsert(ctx, _entry(
        cache_key=key, status=CACHE_STATUS_FAILED,
        error_type="RuntimeError", error_message="boom",
    ))
    cache.upsert(ctx, _entry(
        cache_key=key, status=CACHE_STATUS_COMPLETED,
        artifact_ids=("art-1",),
    ))

    entry = cache.lookup(
        ctx, document_hash="sha256:doc", processor_kind="mock",
    )
    assert entry is not None
    assert entry.status == CACHE_STATUS_COMPLETED
    assert entry.artifact_ids == ("art-1",)


def test_lookup_returns_none_for_missing_key(workspace, ctx):
    cache = JsonlProcessingResultCache(workspace)
    assert cache.lookup(
        ctx, document_hash="sha256:never-seen", processor_kind="mock",
    ) is None


def test_cache_isolates_processor_kinds(workspace, ctx):
    """Two compilers writing for the same document must NOT collide
 — a `completed` entry for one processor kind doesn't satisfy a
 lookup for a different processor kind."""
    cache = JsonlProcessingResultCache(workspace)
    key_a = make_cache_key(document_hash="sha256:doc", processor_kind="parser-a")
    cache.upsert(ctx, _entry(
        cache_key=key_a, status=CACHE_STATUS_COMPLETED,
        artifact_ids=("art-a",), processor_kind="parser-a",
    ))

    hit = cache.lookup(
        ctx, document_hash="sha256:doc", processor_kind="parser-a",
    )
    miss = cache.lookup(
        ctx, document_hash="sha256:doc", processor_kind="parser-b",
    )
    assert hit is not None and hit.artifact_ids == ("art-a",)
    assert miss is None


def test_cache_persists_across_instances(workspace, ctx):
    """Surviving worker restarts is the WHOLE point — verify that a
 new instance reads what an earlier instance wrote."""
    key = make_cache_key(document_hash="sha256:doc", processor_kind="mock")
    JsonlProcessingResultCache(workspace).upsert(ctx, _entry(
        cache_key=key, status=CACHE_STATUS_COMPLETED, artifact_ids=("a",),
    ))

    fresh = JsonlProcessingResultCache(workspace)
    entry = fresh.lookup(
        ctx, document_hash="sha256:doc", processor_kind="mock",
    )
    assert entry is not None
    assert entry.status == CACHE_STATUS_COMPLETED
    assert entry.artifact_ids == ("a",)


def test_processing_status_in_progress_returns_processing(workspace, ctx):
    """If a prior attempt is still in flight (no completed entry),
 the cache reports `processing` — callers can decide whether to
 wait, fail-fast, or proceed (current activity does the latter)."""
    cache = JsonlProcessingResultCache(workspace)
    key = make_cache_key(document_hash="sha256:doc", processor_kind="mock")
    cache.upsert(ctx, _entry(
        cache_key=key, status=CACHE_STATUS_PROCESSING,
    ))
    entry = cache.lookup(
        ctx, document_hash="sha256:doc", processor_kind="mock",
    )
    assert entry is not None
    assert entry.status == CACHE_STATUS_PROCESSING
