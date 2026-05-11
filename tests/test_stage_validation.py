"""Unit tests for the stage-validation contract + per-stage validators.

The validators are pure (no I/O — they take a `read_back` callable
that returns bytes), so each test injects a tiny in-memory map
{artifact_id → bytes} as the read-back. Covers:

 * Empty parse → fails
 * Empty content_inventory → fails (covered by compile validator's
 "no parsed_content_manifest" warning + canonical-kinds check)
 * Zero chunks → fails
 * Chunks artifact registered but storage empty → fails
 * Duplicate chunk ids → fails
 * Tenant/project scope mismatch → fails
 * graph_required=True but graph missing → fails
 * graph references missing chunks → fails
 * enrich_required=True but enrichment missing → fails
 * Skipped enrich/graph passes only when not required
 * Stage cannot be marked succeeded before validation passes (the
 workflow gate enforces this — covered separately in
 test_project_processing_workflow.py)
"""

from __future__ import annotations

from datetime import datetime, timezone

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.results import (
    ARTIFACT_KIND_CHUNK,
    ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
    ARTIFACT_KIND_PARSED_SOURCE,
)
from j1.processing.stage_validation import (
    CHECK_STATUS_FAILED,
    CHECK_STATUS_PASSED,
    CHECK_STATUS_WARNING,
    StageValidationCheck,
    StageValidationResult,
    VALIDATION_STATUS_FAILED,
    VALIDATION_STATUS_PASSED,
    VALIDATION_STATUS_WARNING,
    aggregate_status,
)
from j1.processing.stage_validators import (
    validate_chunks,
    validate_compile,
    validate_enrich,
    validate_graph,
)
from j1.projects.context import ProjectContext


_CTX = ProjectContext(tenant_id="acme", project_id="alpha")


def _artifact(
    *, artifact_id: str, kind: str, run_id: str = "run-1",
    document_id: str = "doc-1",
    source_artifact_ids: list[str] | None = None,
    project: ProjectContext = _CTX,
) -> ArtifactRecord:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=project,
        kind=kind,
        location=f"compiled/{artifact_id}.json",
        content_hash=f"h-{artifact_id}",
        byte_size=100,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[document_id],
        source_artifact_ids=source_artifact_ids or [],
        metadata={"run_id": run_id},
    )


def _read_from_map(
    contents: dict[str, bytes | None],
):
    """Make a `read_back` closure backed by an in-memory dict.
 `None` value simulates "registered but storage missing"."""
    def _read(artifact: ArtifactRecord) -> bytes | None:
        return contents.get(artifact.artifact_id)
    return _read


# ---- aggregate_status -------------------------------------------


def test_aggregate_status_empty_returns_passed():
    """A stage with no checks is trivially valid (rare in practice
 — every durable stage has at least an artifact-existence check
 — but the empty-list path must terminate cleanly)."""
    assert aggregate_status([]) == VALIDATION_STATUS_PASSED


def test_aggregate_status_any_failed_returns_failed():
    checks = [
        StageValidationCheck(name="a", status=CHECK_STATUS_PASSED),
        StageValidationCheck(name="b", status=CHECK_STATUS_FAILED),
        StageValidationCheck(name="c", status=CHECK_STATUS_WARNING),
    ]
    assert aggregate_status(checks) == VALIDATION_STATUS_FAILED


def test_aggregate_status_warning_short_circuits_passed():
    checks = [
        StageValidationCheck(name="a", status=CHECK_STATUS_PASSED),
        StageValidationCheck(name="b", status=CHECK_STATUS_WARNING),
    ]
    assert aggregate_status(checks) == VALIDATION_STATUS_WARNING


def test_stage_validation_result_passed_includes_warning():
    """`warning` is non-blocking — `passed` returns True so the
 workflow records COMPLETED. Only `failed` blocks."""
    r_warn = StageValidationResult(
        stage_name="compile", run_id="r1", document_id="d1",
        tenant_id="acme", project_id="alpha", workspace_id=None,
        attempt=1, validation_status=VALIDATION_STATUS_WARNING,
    )
    r_fail = StageValidationResult(
        stage_name="compile", run_id="r1", document_id="d1",
        tenant_id="acme", project_id="alpha", workspace_id=None,
        attempt=1, validation_status=VALIDATION_STATUS_FAILED,
    )
    assert r_warn.passed() is True
    assert r_fail.passed() is False


def test_to_payload_round_trips():
    """The persisted JSON shape must contain every operationally-
 interesting field. Locking it with a test guards against an
 accidental rename breaking external consumers (audit dashboards)."""
    r = StageValidationResult(
        stage_name="compile", run_id="r1", document_id="d1",
        tenant_id="acme", project_id="alpha", workspace_id="ws1",
        attempt=2, validation_status=VALIDATION_STATUS_PASSED,
        checks=[StageValidationCheck("c1", CHECK_STATUS_PASSED, "ok")],
        errors=["e1"], warnings=["w1"],
        output_refs=["a1"], artifact_refs=["a1"],
    )
    payload = r.to_payload()
    assert payload["stage_name"] == "compile"
    assert payload["validation_status"] == "passed"
    assert payload["attempt"] == 2
    assert payload["checks"] == [
        {"name": "c1", "status": "passed", "message": "ok"},
    ]
    assert payload["errors"] == ["e1"]
    assert payload["output_refs"] == ["a1"]


# ---- validate_compile -------------------------------------------


def test_compile_zero_artifacts_fails():
    """Compile reported succeeded but produced nothing — workflow
 must not proceed to downstream stages with empty input."""
    checks = validate_compile(
        artifacts=[],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="r1", expected_document_id="d1",
        read_back=_read_from_map({}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "compile_artifacts_present"
        for c in checks
    )


def test_compile_no_canonical_kinds_fails():
    """Compile produced artifacts but none of the canonical kinds
 (parsed_source / parsed_content_manifest / chunk) — downstream
 stages will see no input."""
    a = _artifact(artifact_id="a1", kind="some.weird.kind")
    checks = validate_compile(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="r1", expected_document_id="doc-1",
        read_back=_read_from_map({"a1": b'{"x": 1}'}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "compile_canonical_kinds"
        for c in checks
    )


def test_compile_storage_missing_fails():
    """Artifact is registered, but the file on disk doesn't exist
 (read-back returns None). This catches "registry write succeeded,
 file write failed" inconsistencies."""
    a = _artifact(artifact_id="a1", kind=ARTIFACT_KIND_CHUNK)
    checks = validate_compile(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="r1", expected_document_id="doc-1",
        read_back=_read_from_map({"a1": None}),  # storage missing
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "artifact_readable"
        for c in checks
    )


def test_compile_zero_byte_file_fails():
    """File exists but is zero bytes — empty artifact slipped
 through. Distinct from the missing-file failure."""
    a = _artifact(artifact_id="a1", kind=ARTIFACT_KIND_CHUNK)
    checks = validate_compile(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="r1", expected_document_id="doc-1",
        read_back=_read_from_map({"a1": b""}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "artifact_non_empty"
        for c in checks
    )


def test_compile_tenant_mismatch_fails():
    """Artifact landed in the wrong tenant — defense against cross-
 tenant bleed bugs."""
    other = ProjectContext(tenant_id="megacorp", project_id="alpha")
    a = _artifact(artifact_id="a1", kind=ARTIFACT_KIND_CHUNK, project=other)
    checks = validate_compile(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="r1", expected_document_id="doc-1",
        read_back=_read_from_map({"a1": b'{"x": 1}'}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "scope_tenant_match"
        for c in checks
    )


def test_compile_passes_with_canonical_kinds_and_readable_artifacts():
    """Happy path: parsed_source + parsed_content_manifest + chunk
 all present, all readable, all scoped correctly."""
    artifacts = [
        _artifact(artifact_id="ps", kind=ARTIFACT_KIND_PARSED_SOURCE),
        _artifact(artifact_id="m1", kind=ARTIFACT_KIND_PARSED_CONTENT_MANIFEST),
        _artifact(artifact_id="c1", kind=ARTIFACT_KIND_CHUNK),
    ]
    contents = {
        "ps": b'{"content_list": []}',
        "m1": b'{"sections": []}',
        "c1": b'[{"id": "c1#0", "body": "hello"}]',
    }
    checks = validate_compile(
        artifacts=artifacts,
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        read_back=_read_from_map(contents),
    )
    # No failed checks.
    assert not any(c.status == CHECK_STATUS_FAILED for c in checks)
    assert aggregate_status(checks) in (
        VALIDATION_STATUS_PASSED, VALIDATION_STATUS_WARNING,
    )


# ---- validate_chunks --------------------------------------------


def test_chunks_zero_chunks_artifact_fails():
    """No chunk-kind artifacts at all — downstream graph + index
 have nothing to consume."""
    checks = validate_chunks(
        artifacts=[],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        read_back=_read_from_map({}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "chunk_artifacts_present"
        for c in checks
    )


def test_chunks_artifact_exists_but_storage_empty_fails():
    """Chunk artifact registered, file missing on disk — catches
 "wrote registry record before persisting bytes" race."""
    a = _artifact(artifact_id="c1", kind=ARTIFACT_KIND_CHUNK)
    checks = validate_chunks(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        read_back=_read_from_map({"c1": None}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "artifact_readable"
        for c in checks
    )


def test_chunks_artifact_parses_to_zero_chunks_fails():
    """Artifact reads as empty list — count > 0 rule trips."""
    a = _artifact(artifact_id="c1", kind=ARTIFACT_KIND_CHUNK)
    checks = validate_chunks(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        read_back=_read_from_map({"c1": b'[]'}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and (
            c.name == "chunk_artifact_non_empty"
            or c.name == "chunk_count_positive"
        )
        for c in checks
    )


def test_chunks_duplicate_ids_fail():
    """Chunk ids must be unique across the run — duplicates would
 let downstream stages confuse provenance."""
    a = _artifact(artifact_id="c1", kind=ARTIFACT_KIND_CHUNK)
    payload = b'[{"id":"x","body":"a"},{"id":"x","body":"b"}]'
    checks = validate_chunks(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        read_back=_read_from_map({"c1": payload}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "chunk_ids_unique"
        for c in checks
    )


def test_chunks_all_empty_bodies_fails():
    """Every chunk has empty body/content — parser regression
 likely. Distinct from the count > 0 check (count can be > 0
 while every chunk is empty text)."""
    a = _artifact(artifact_id="c1", kind=ARTIFACT_KIND_CHUNK)
    payload = b'[{"id":"x","body":""},{"id":"y","body":"   "}]'
    checks = validate_chunks(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        read_back=_read_from_map({"c1": payload}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "chunk_text_present"
        for c in checks
    )


def test_chunks_some_empty_bodies_warns():
    """Some chunks empty + some populated → warning, not failure.
 Stage still succeeds."""
    a = _artifact(artifact_id="c1", kind=ARTIFACT_KIND_CHUNK)
    payload = b'[{"id":"x","body":"hello"},{"id":"y","body":""}]'
    checks = validate_chunks(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        read_back=_read_from_map({"c1": payload}),
    )
    text_check = next(
        c for c in checks if c.name == "chunk_text_present"
    )
    assert text_check.status == CHECK_STATUS_WARNING
    assert aggregate_status(checks) == VALIDATION_STATUS_WARNING


def test_chunks_happy_path_passes():
    a = _artifact(artifact_id="c1", kind=ARTIFACT_KIND_CHUNK)
    payload = (
        b'[{"id":"x","body":"hello"},'
        b'{"id":"y","body":"world"},'
        b'{"id":"z","body":"again"}]'
    )
    checks = validate_chunks(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        read_back=_read_from_map({"c1": payload}),
    )
    assert not any(c.status == CHECK_STATUS_FAILED for c in checks)


# ---- validate_enrich --------------------------------------------


def test_enrich_required_but_missing_fails():
    """Operator asked for enrich (planner or caller decision) but
 no enriched artifacts produced — required-step contract."""
    checks = validate_enrich(
        artifacts=[],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        enrich_required=True,
        read_back=_read_from_map({}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "enrich_artifacts_present"
        for c in checks
    )


def test_enrich_skipped_passes_with_no_artifacts():
    """enrich_required=False and no enriched artifacts → passes.
 The skip path's audit trail is the SKIPPED step record (not
 this validator's concern)."""
    checks = validate_enrich(
        artifacts=[],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        enrich_required=False,
        read_back=_read_from_map({}),
    )
    assert aggregate_status(checks) == VALIDATION_STATUS_PASSED


def test_enrich_artifact_without_upstream_link_fails():
    """Enriched artifact with empty source_artifact_ids — orphaned
 from the upstream chunk, can't trace lineage."""
    a = _artifact(
        artifact_id="e1", kind="enriched.tables",
        source_artifact_ids=[],  # explicitly empty
    )
    checks = validate_enrich(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        enrich_required=True,
        read_back=_read_from_map({"e1": b'{"tables": []}'}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "enrich_links_upstream"
        for c in checks
    )


# ---- validate_graph ---------------------------------------------


def test_graph_required_but_missing_fails():
    checks = validate_graph(
        artifacts=[],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        graph_required=True, chunk_artifact_ids=set(),
        read_back=_read_from_map({}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "graph_artifact_present"
        for c in checks
    )


def test_graph_zero_nodes_fails():
    """Graph stage said succeeded but graph has 0 nodes — graph
 isn't grounded in any entities."""
    a = _artifact(artifact_id="g1", kind="graph_json")
    payload = b'{"nodes": [], "edges": []}'
    checks = validate_graph(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        graph_required=True, chunk_artifact_ids=set(),
        read_back=_read_from_map({"g1": payload}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "graph_node_count_positive"
        for c in checks
    )


def test_graph_dangling_edges_fail():
    """Edges reference node ids that aren't in the nodes list —
 invalid graph topology. Index would surface dangling references
 at retrieval time."""
    a = _artifact(artifact_id="g1", kind="graph_json")
    payload = (
        b'{"nodes": [{"id": "n1"}, {"id": "n2"}], '
        b'"edges": [{"source": "n1", "target": "ghost"}]}'
    )
    checks = validate_graph(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        graph_required=True, chunk_artifact_ids=set(),
        read_back=_read_from_map({"g1": payload}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "graph_edges_reference_nodes"
        for c in checks
    )


def test_graph_grounded_in_chunks_fails_when_source_artifacts_missing():
    """Graph carries source_artifact_ids that don't match this run's
 chunks — orphan graph from a different run that snuck through."""
    a = _artifact(
        artifact_id="g1", kind="graph_json",
        source_artifact_ids=["chunk-from-other-run"],
    )
    payload = b'{"nodes": [{"id": "n1"}], "edges": []}'
    checks = validate_graph(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        graph_required=True,
        chunk_artifact_ids={"chunk-from-this-run"},
        read_back=_read_from_map({"g1": payload}),
    )
    assert any(
        c.status == CHECK_STATUS_FAILED
        and c.name == "graph_grounded_in_chunks"
        for c in checks
    )


def test_graph_skipped_passes_when_not_required():
    checks = validate_graph(
        artifacts=[],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        graph_required=False, chunk_artifact_ids=set(),
        read_back=_read_from_map({}),
    )
    assert aggregate_status(checks) == VALIDATION_STATUS_PASSED


def test_graph_happy_path_passes():
    a = _artifact(
        artifact_id="g1", kind="graph_json",
        source_artifact_ids=["chunk-1"],
    )
    payload = (
        b'{"nodes": [{"id": "n1"}, {"id": "n2"}], '
        b'"edges": [{"source": "n1", "target": "n2"}]}'
    )
    checks = validate_graph(
        artifacts=[a],
        expected_tenant="acme", expected_project="alpha",
        expected_run_id="run-1", expected_document_id="doc-1",
        graph_required=True, chunk_artifact_ids={"chunk-1"},
        read_back=_read_from_map({"g1": payload}),
    )
    assert not any(c.status == CHECK_STATUS_FAILED for c in checks)
