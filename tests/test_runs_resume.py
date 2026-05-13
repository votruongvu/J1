"""Unit tests for `j1.runs.resume` — settings hash, compatibility,
diff, and snapshot construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from j1.runs.resume import (
    RESUMABLE_STAGES,
    RESUME_SETTINGS_FIELDS,
    build_resume_snapshot,
    build_settings_snapshot,
    compatible_settings,
    compute_settings_hash,
    settings_diff,
)


@dataclass
class _StubRequest:
    """Duck-types `ProjectProcessingRequest` for the fields the helpers
 need. Keeps the test surface independent of workflow imports."""
    compiler_kind: str = "raganything"
    enricher_kind: str | None = "composite_enricher"
    graph_builder_kind: str | None = "lightrag_graph"
    indexer_kind: str | None = "sqlite_search"
    planner_enabled: bool = True
    policy: str = "auto"
    domain_override: str | None = None
    workspace_default_domain: str | None = None
    failure_policy: str = "fail_fast"


def test_settings_snapshot_captures_every_resume_field():
    snapshot = build_settings_snapshot(_StubRequest())
    # Snapshot keys exactly match the canonical field set — no extras,
    # no missing fields. Drift here would silently invalidate the
    # compatibility check.
    assert set(snapshot.keys()) == set(RESUME_SETTINGS_FIELDS)


def test_compute_settings_hash_is_order_stable():
    """Hash MUST be invariant to dict key order — operators may build
 the dict from FastAPI body / env / defaults in any order."""
    snap_a = build_settings_snapshot(_StubRequest())
    snap_b = {k: snap_a[k] for k in reversed(list(snap_a.keys()))}
    assert compute_settings_hash(snap_a) == compute_settings_hash(snap_b)


def test_compute_settings_hash_normalises_strenum_vs_str():
    """A `StrEnum` wrapper compares equal to the bare string — the
 workflow's `policy` field is a `StrEnum`, the REST request body
 is a plain string. Same hash."""
    from enum import StrEnum

    class _Policy(StrEnum):
        AUTO = "auto"

    enum_snap = build_settings_snapshot(_StubRequest(policy=_Policy.AUTO))
    str_snap = build_settings_snapshot(_StubRequest(policy="auto"))
    assert compute_settings_hash(enum_snap) == compute_settings_hash(str_snap)


def test_compatible_settings_true_when_all_fields_match():
    a = build_settings_snapshot(_StubRequest())
    b = build_settings_snapshot(_StubRequest())
    assert compatible_settings(a, b) is True


def test_compatible_settings_false_on_processor_kind_drift():
    a = build_settings_snapshot(_StubRequest())
    b = build_settings_snapshot(_StubRequest(enricher_kind="other"))
    assert compatible_settings(a, b) is False


def test_settings_diff_returns_only_changed_fields():
    a = build_settings_snapshot(_StubRequest(enricher_kind="x", indexer_kind="sqlite_search"))
    b = build_settings_snapshot(_StubRequest(enricher_kind="y", indexer_kind="qdrant_search"))
    diff = settings_diff(a, b)
    assert set(diff.keys()) == {"enricher_kind", "indexer_kind"}
    assert diff["enricher_kind"] == {"before": "x", "after": "y"}
    assert diff["indexer_kind"] == {"before": "sqlite_search", "after": "qdrant_search"}


def test_settings_diff_empty_when_compatible():
    a = build_settings_snapshot(_StubRequest())
    b = build_settings_snapshot(_StubRequest())
    assert settings_diff(a, b) == {}


def test_resumable_stages_is_narrow():
    """Only enrich + graph are resumable in v1. Compile, chunks, and
 index always re-run — see RESUMABLE_STAGES docstring for the why.
 Locking the set with a test guards against accidental widening
 that would skip stages whose outputs aren't actually persisted
 across runs."""
    assert RESUMABLE_STAGES == frozenset({"enrich", "graph"})


def test_build_resume_snapshot_partitions_completed_vs_failed():
    """`step_results_payload` is the workflow's flat list of step
 outcomes; the snapshot partitions them so the resume endpoint
 can answer "what's safe to skip" without re-walking the list."""
    snap = build_resume_snapshot(
        request=_StubRequest(),
        step_results_payload=[
            {"step": "compile", "status": "completed"},
            {"step": "enrich", "status": "completed"},
            {"step": "graph", "status": "failed"},
            {"step": "index", "status": "skipped"},
        ],
        produced_artifact_ids=["a", "b", "c"],
        produced_artifact_kinds=["chunk", "enriched.tables", "graph_json"],
        snapshot_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert snap["completed_steps"] == ["compile", "enrich"]
    assert snap["failed_steps"] == ["graph"]
    assert snap["produced_artifact_ids"] == ["a", "b", "c"]
    assert snap["produced_artifact_kinds"] == [
        "chunk", "enriched.tables", "graph_json",
    ]
    assert snap["snapshot_at"] == "2026-05-10T12:00:00+00:00"
    assert snap["settings_hash"] == compute_settings_hash(
        snap["settings_snapshot"]
    )


def test_build_resume_snapshot_carries_failure_context():
    snap = build_resume_snapshot(
        request=_StubRequest(),
        step_results_payload=[],
        produced_artifact_ids=[],
        produced_artifact_kinds=[],
        failure_code="REQUIRED_STEP_FAILED",
        failure_message="enrich failed for artifact-1: provider timeout",
    )
    assert snap["failure_code"] == "REQUIRED_STEP_FAILED"
    assert snap["failure_message"].startswith("enrich failed")


def test_build_resume_snapshot_carries_per_step_identity():
    """``completed_step_instances`` adds artifact-level identity so a
    resume consumer can disambiguate the N "enrich" entries you'd
    see on a multi-artifact document. Without this, ``completed_steps``
    looks like ``[\"enrich\", \"enrich\", \"enrich\"]`` and a
    consumer can't tell which compile artifacts already had
    enrichment carried forward — issue-8 fix."""
    snap = build_resume_snapshot(
        request=_StubRequest(),
        step_results_payload=[
            {"step": "compile", "status": "completed",
             "metadata": {"document_id": "doc-1"}},
            {"step": "enrich", "status": "completed",
             "metadata": {"artifact_id": "art-1",
                          "document_id": "doc-1"}},
            {"step": "enrich", "status": "completed",
             "metadata": {"artifact_id": "art-2",
                          "document_id": "doc-1"}},
            {"step": "enrich", "status": "completed",
             "metadata": {"artifact_id": "art-3",
                          "document_id": "doc-1"}},
        ],
        produced_artifact_ids=[],
        produced_artifact_kinds=[],
    )
    # Legacy view still carries 3 "enrich" entries for backward
    # compat — consumers that already use it don't break.
    assert snap["completed_steps"].count("enrich") == 3
    # Identity-aware view distinguishes the three enrich attempts.
    instances = snap["completed_step_instances"]
    enrich_instances = [
        i for i in instances if i["step"] == "enrich"
    ]
    assert len(enrich_instances) == 3
    artifact_ids = {i["artifact_id"] for i in enrich_instances}
    assert artifact_ids == {"art-1", "art-2", "art-3"}
    assert all(i["document_id"] == "doc-1" for i in enrich_instances)
