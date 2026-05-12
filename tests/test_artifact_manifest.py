"""Phase 9 — `ArtifactManifest` contract + safe-reuse predicate.

The manifest is the first-class record of "what artifacts did this
run produce or reuse?" — see `j1.artifacts.manifest` docstring for
the design rationale. This file pins the contract: the
serialisation round-trip, the per-run identity rules, and the
safe-reuse predicate that decides whether a prior run's artifact
can be referenced by a new run.

The reuse predicate is the load-bearing piece. The spec section-12
rules are easy to get subtly wrong (e.g. "should two None facets
count as a match?" — no, neither side has enough info to OK
reuse). Every rule gets a dedicated test name so a regression is
obvious in the failure output.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.manifest import (
    ArtifactManifest,
    JsonlArtifactManifestStore,
    MANIFEST_FILENAME,
    ManifestArtifactRef,
    REUSE_REASON_SAME_COMPILE_CONFIG,
    REUSE_REASON_SAME_DOCUMENT_VERSION,
    REUSE_REASON_SAME_FILE_HASH,
    ReuseContext,
    build_manifest,
    is_safe_to_reuse,
)
from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def _artifact(
    *, ctx: ProjectContext, artifact_id: str,
    metadata: dict | None = None,
    kind: str = "chunk",
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"compiled/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}",
        byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata=metadata or {},
    )


# ---- Safe-reuse predicate -----------------------------------------


def test_reuse_safe_when_document_version_matches(ctx):
    """Strongest facet: same document_version_id. Spec rule 1."""
    prior = _artifact(
        ctx=ctx, artifact_id="a-1",
        metadata={"document_version_id": "dv-1"},
    )
    safe, reason = is_safe_to_reuse(
        prior_artifact=prior,
        current=ReuseContext(document_version_id="dv-1"),
    )
    assert safe is True
    assert reason == REUSE_REASON_SAME_DOCUMENT_VERSION


def test_reuse_unsafe_when_document_version_differs(ctx):
    """Mismatched document version → hard reject. Carrying forward
 an artifact from a different version would silently surface stale
 content."""
    prior = _artifact(
        ctx=ctx, artifact_id="a-1",
        metadata={"document_version_id": "dv-1"},
    )
    safe, reason = is_safe_to_reuse(
        prior_artifact=prior,
        current=ReuseContext(document_version_id="dv-2"),
    )
    assert safe is False
    assert reason == ""


def test_reuse_safe_when_file_hash_matches(ctx):
    """Second-tier: same file_hash. Same content → same compiled
 artifacts, even when the artifact-level document_version_id was
 not recorded by an older producer."""
    prior = _artifact(
        ctx=ctx, artifact_id="a-1",
        metadata={"file_hash": "sha256:aaa"},
    )
    safe, reason = is_safe_to_reuse(
        prior_artifact=prior,
        current=ReuseContext(file_hash="sha256:aaa"),
    )
    assert safe is True
    assert reason == REUSE_REASON_SAME_FILE_HASH


def test_reuse_unsafe_when_compile_config_differs(ctx):
    """Compile-config-hash mismatch is a strict rejection — same
 file, different compile settings (e.g. different parser flags)
 produces different artifacts. Reusing across that boundary leaks
 stale data."""
    prior = _artifact(
        ctx=ctx, artifact_id="a-1",
        metadata={
            "document_version_id": "dv-1",
            "compile_config_hash": "hash-old",
        },
    )
    safe, _reason = is_safe_to_reuse(
        prior_artifact=prior,
        current=ReuseContext(
            document_version_id="dv-1",
            compile_config_hash="hash-new",
        ),
    )
    assert safe is False


def test_reuse_unsafe_when_removed_document_artifact(ctx):
    """The headline contract from spec section 12: 'Never let removed
 document artifacts be reused by default.' Even when every other
 facet matches, an artifact stamped knowledge_state=removed must
 NOT be eligible for reuse."""
    prior = _artifact(
        ctx=ctx, artifact_id="a-removed",
        metadata={
            "document_version_id": "dv-1",
            "file_hash": "sha256:aaa",
            "compile_config_hash": "hash-1",
            "knowledge_state": "removed",  # Phase 3 stamp
        },
    )
    safe, reason = is_safe_to_reuse(
        prior_artifact=prior,
        current=ReuseContext(
            document_version_id="dv-1",
            file_hash="sha256:aaa",
            compile_config_hash="hash-1",
        ),
    )
    assert safe is False
    assert reason == ""


def test_reuse_unsafe_when_no_facets_recorded(ctx):
    """An artifact written by a producer that didn't stamp any
 reuse-relevant metadata is NOT eligible for reuse — we have no
 way to reason about whether it's safe. Strict-by-default rule."""
    prior = _artifact(ctx=ctx, artifact_id="a-1", metadata={})
    safe, _reason = is_safe_to_reuse(
        prior_artifact=prior,
        current=ReuseContext(document_version_id="dv-1"),
    )
    assert safe is False


def test_reuse_unaffected_by_facet_with_one_none_side(ctx):
    """Backward compat: a facet None on EITHER side is "no
 constraint" — other facets can still grant reuse. Lets newer
 runs reuse older artifacts that predate a new metadata field
 the spec added later."""
    prior = _artifact(
        ctx=ctx, artifact_id="a-1",
        metadata={
            "document_version_id": "dv-1",
            # No domain_config_version stamped on this prior artifact.
        },
    )
    safe, reason = is_safe_to_reuse(
        prior_artifact=prior,
        current=ReuseContext(
            document_version_id="dv-1",
            domain_config_version="civil-engineering-v3",  # current has it
        ),
    )
    # Reuse proceeds because document_version_id matches; the
    # domain_config_version facet was None on the prior side so it
    # doesn't block.
    assert safe is True
    assert reason == REUSE_REASON_SAME_DOCUMENT_VERSION


def test_reuse_returns_strongest_reason_when_multiple_match(ctx):
    """Reason precedence: document_version_id > file_hash > compile_
 config. When multiple match, the strongest one becomes the
 manifest's `reuse_reason` so the audit trail shows the most
 specific provenance."""
    prior = _artifact(
        ctx=ctx, artifact_id="a-1",
        metadata={
            "document_version_id": "dv-1",
            "file_hash": "sha256:aaa",
            "compile_config_hash": "hash-1",
        },
    )
    safe, reason = is_safe_to_reuse(
        prior_artifact=prior,
        current=ReuseContext(
            document_version_id="dv-1",
            file_hash="sha256:aaa",
            compile_config_hash="hash-1",
        ),
    )
    assert safe is True
    assert reason == REUSE_REASON_SAME_DOCUMENT_VERSION


# ---- Manifest builder ---------------------------------------------


def test_build_manifest_partitions_produced_vs_reused(ctx):
    """Produced artifacts ride along under `produced_by_this_run=True`;
 reused artifacts carry `reused_from_run_id` + `reuse_reason` so
 the audit trail is self-explaining."""
    produced = [_artifact(ctx=ctx, artifact_id="a-produced")]
    reused = [_artifact(ctx=ctx, artifact_id="a-reused")]
    manifest = build_manifest(
        manifest_id="m-1",
        run_id="r-new",
        document_id="doc-1",
        document_version_id="dv-1",
        produced=produced,
        reused=reused,
        reused_from_run_id="r-prev",
        reuse_reasons={"a-reused": REUSE_REASON_SAME_DOCUMENT_VERSION},
        now=_NOW,
    )
    assert manifest.run_id == "r-new"
    assert manifest.document_id == "doc-1"
    assert manifest.document_version_id == "dv-1"
    assert manifest.reused_from_run_id == "r-prev"
    assert manifest.produced_artifact_ids() == ("a-produced",)
    assert manifest.reused_artifact_ids() == ("a-reused",)
    reused_ref = next(a for a in manifest.artifacts if not a.produced_by_this_run)
    assert reused_ref.reuse_reason == REUSE_REASON_SAME_DOCUMENT_VERSION
    assert reused_ref.reused_from_run_id == "r-prev"


def test_build_manifest_clears_reused_from_when_nothing_reused(ctx):
    """`reused_from_run_id` is a convenience pointer for "did we
 inherit anything?" — when the run produced everything fresh,
 the pointer is None even if the caller passed one (defensive)."""
    manifest = build_manifest(
        manifest_id="m-1", run_id="r-1", document_id="doc-1",
        document_version_id="dv-1",
        produced=[_artifact(ctx=ctx, artifact_id="a-1")],
        reused=[],
        reused_from_run_id="r-prev",  # ignored when nothing actually reused
        now=_NOW,
    )
    assert manifest.reused_from_run_id is None


# ---- Manifest store -----------------------------------------------


def test_store_upsert_then_get_round_trips(workspace, ctx):
    store = JsonlArtifactManifestStore(workspace)
    manifest = build_manifest(
        manifest_id="m-1", run_id="r-1", document_id="doc-1",
        document_version_id="dv-1",
        produced=[_artifact(ctx=ctx, artifact_id="a-1")],
        now=_NOW,
    )
    store.upsert(ctx, manifest)
    loaded = store.get(ctx, "m-1")
    assert loaded is not None
    assert loaded.run_id == "r-1"
    assert loaded.document_id == "doc-1"
    assert loaded.artifacts[0].artifact_id == "a-1"


def test_store_get_for_run_returns_latest_snapshot(workspace, ctx):
    """JSONL: the same manifest_id may be appended multiple times
 (e.g. on a continue-as-new boundary the workflow re-writes
 the manifest). `get_for_run` returns the latest snapshot."""
    store = JsonlArtifactManifestStore(workspace)
    # First snapshot — one artifact.
    store.upsert(ctx, build_manifest(
        manifest_id="m-1", run_id="r-1", document_id="doc-1",
        document_version_id="dv-1",
        produced=[_artifact(ctx=ctx, artifact_id="a-1")],
        now=_NOW,
    ))
    # Second snapshot under the same manifest_id/run_id — two
    # artifacts. The store should surface the latest.
    store.upsert(ctx, build_manifest(
        manifest_id="m-1", run_id="r-1", document_id="doc-1",
        document_version_id="dv-1",
        produced=[
            _artifact(ctx=ctx, artifact_id="a-1"),
            _artifact(ctx=ctx, artifact_id="a-2"),
        ],
        now=_NOW,
    ))
    latest = store.get_for_run(ctx, "r-1")
    assert latest is not None
    assert {a.artifact_id for a in latest.artifacts} == {"a-1", "a-2"}


def test_store_list_for_document_returns_per_run_latest_sorted(
    workspace, ctx,
):
    """`list_for_document` is the document-detail page's underlying
 query — manifests for one document, latest-per-run, sorted by
 created_at descending."""
    store = JsonlArtifactManifestStore(workspace)
    earlier = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    store.upsert(ctx, build_manifest(
        manifest_id="m-1", run_id="r-1", document_id="doc-1",
        document_version_id="dv-1", produced=[], now=earlier,
    ))
    store.upsert(ctx, build_manifest(
        manifest_id="m-2", run_id="r-2", document_id="doc-1",
        document_version_id="dv-1", produced=[], now=later,
    ))
    # Different document — must NOT appear in doc-1's listing.
    store.upsert(ctx, build_manifest(
        manifest_id="m-3", run_id="r-3", document_id="doc-other",
        document_version_id="dv-2", produced=[], now=later,
    ))
    rows = store.list_for_document(ctx, "doc-1")
    assert [m.run_id for m in rows] == ["r-2", "r-1"]


def test_store_get_returns_none_for_missing(workspace, ctx):
    store = JsonlArtifactManifestStore(workspace)
    assert store.get(ctx, "missing") is None
    assert store.get_for_run(ctx, "missing") is None
    assert store.list_for_document(ctx, "missing") == []


def test_store_tolerates_malformed_json_lines(workspace, ctx):
    """JSONL forward-compat: a malformed line shouldn't crash the
 reader. Common case: a partial write that crashed before the
 newline. The store skips the bad line and keeps reading."""
    store = JsonlArtifactManifestStore(workspace)
    store.upsert(ctx, build_manifest(
        manifest_id="m-1", run_id="r-1", document_id="doc-1",
        document_version_id="dv-1", produced=[], now=_NOW,
    ))
    # Append a malformed line directly to the file.
    path = workspace.area(ctx, _audit_area()) / MANIFEST_FILENAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    # Reader should still return our good manifest.
    assert store.get(ctx, "m-1") is not None


# ---- Helper -------------------------------------------------------


def _audit_area():
    from j1.workspace.layout import WorkspaceArea
    return WorkspaceArea.AUDIT
