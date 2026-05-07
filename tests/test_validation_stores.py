"""Tests for `JsonlValidationSetStore` and `JsonlValidationRunStore`.

Both stores follow the `JsonlIngestionRunStore` pattern: append-only
writes, latest-snapshot-wins reads, JSONL on disk under the
workspace's `validation` area. The tests lock in:

  * round-tripping a typed DTO through the file (no field drops)
  * latest-snapshot semantics on repeated upserts
  * cross-tenant + cross-project isolation
  * tolerant reads (malformed line skipped, not propagated)
  * `list_for_run` filters + ordering
"""

from __future__ import annotations

from j1.projects.context import ProjectContext
from j1.validation import (
    JsonlValidationRunStore,
    JsonlValidationSetStore,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationCoverageDTO,
    ValidationResultDTO,
    ValidationRunDTO,
    ValidationSetDTO,
    ValidationSummaryDTO,
    ValidationTestCaseDTO,
)
from j1.validation.dtos import RetrievedChunkRefDTO


def _make_set(
    *,
    set_id: str = "vs-1",
    run_id: str = "run-1",
    created_at: str = "2026-05-07T10:00:00Z",
    test_cases: list[ValidationTestCaseDTO] | None = None,
) -> ValidationSetDTO:
    return ValidationSetDTO(
        validation_set_id=set_id,
        run_id=run_id,
        document_ids=["doc-1"],
        source="generated",
        status="draft",
        created_at=created_at,
        created_by="tester@example.com",
        generator_version="v1",
        artifacts_content_hash="sha256:abcd",
        test_cases=test_cases or [],
        metadata={"note": "test"},
    )


def _make_test_case(
    *,
    test_case_id: str = "tc-1",
    question: str = "What is X?",
    type: str = "retrieval",
    expected_chunks: list[str] | None = None,
) -> ValidationTestCaseDTO:
    return ValidationTestCaseDTO(
        test_case_id=test_case_id,
        question=question,
        type=type,  # type: ignore[arg-type]
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_chunks=expected_chunks or [],
        citation_required=False,
    )


def _make_run(
    *,
    vrun_id: str = "vrun-1",
    vset_id: str = "vs-1",
    run_id: str = "run-1",
    execution_status: str = "completed",
    validation_status: str = "passed",
    started_at: str = "2026-05-07T10:00:00Z",
    completed_at: str | None = "2026-05-07T10:00:05Z",
    summary: ValidationSummaryDTO | None = None,
    results: list[ValidationResultDTO] | None = None,
) -> ValidationRunDTO:
    return ValidationRunDTO(
        validation_run_id=vrun_id,
        validation_set_id=vset_id,
        run_id=run_id,
        execution_status=execution_status,  # type: ignore[arg-type]
        validation_status=validation_status,  # type: ignore[arg-type]
        started_at=started_at,
        completed_at=completed_at,
        actor="tester@example.com",
        summary=summary or ValidationSummaryDTO(
            total=2, passed=2, warning=0, failed=0, skipped=0,
            coverage=ValidationCoverageDTO(by_type={"retrieval": 2}),
        ),
        results=results or [],
    )


# ---- ValidationSetStore --------------------------------------------


def test_set_store_round_trips_test_cases(workspace, ctx):
    """Typed DTO → JSONL → typed DTO without losing fields. Test
    cases are the most field-rich shape in this surface; if any of
    their lists silently drop, every downstream tool sees stale
    state."""
    store = JsonlValidationSetStore(workspace)
    cases = [
        _make_test_case(test_case_id="tc-1", expected_chunks=["c-1", "c-2"]),
        _make_test_case(test_case_id="tc-2", type="answer"),
    ]
    vset = _make_set(test_cases=cases)
    store.upsert(ctx, vset)

    fetched = store.get(ctx, "vs-1")
    assert fetched is not None
    assert fetched.validation_set_id == "vs-1"
    assert fetched.run_id == "run-1"
    assert fetched.source == "generated"
    assert fetched.status == "draft"
    assert len(fetched.test_cases) == 2
    assert fetched.test_cases[0].test_case_id == "tc-1"
    assert fetched.test_cases[0].expected_chunks == ["c-1", "c-2"]
    assert fetched.test_cases[1].type == "answer"
    assert fetched.metadata == {"note": "test"}


def test_set_store_latest_snapshot_wins(workspace, ctx):
    """Re-upserting the same id replaces the previous snapshot
    semantically. Phase 5 will use this to support editing — the
    pattern is locked here so the contract is honest from day one."""
    store = JsonlValidationSetStore(workspace)
    store.upsert(ctx, _make_set(set_id="vs-1", created_at="2026-01-01T00:00:00Z"))
    store.upsert(ctx, _make_set(set_id="vs-1", created_at="2026-05-07T10:00:00Z"))

    fetched = store.get(ctx, "vs-1")
    assert fetched is not None
    assert fetched.created_at == "2026-05-07T10:00:00Z"


def test_set_store_get_missing_returns_none(workspace, ctx):
    store = JsonlValidationSetStore(workspace)
    assert store.get(ctx, "does-not-exist") is None


def test_set_store_list_for_run_filters_and_orders(workspace, ctx):
    """List should return only sets for the requested run, ordered
    most-recent-first by created_at — that's how the FE renders
    the dropdown."""
    store = JsonlValidationSetStore(workspace)
    store.upsert(ctx, _make_set(set_id="vs-A1", run_id="run-A", created_at="2026-01-01T00:00:00Z"))
    store.upsert(ctx, _make_set(set_id="vs-A2", run_id="run-A", created_at="2026-05-07T10:00:00Z"))
    store.upsert(ctx, _make_set(set_id="vs-B1", run_id="run-B", created_at="2026-04-01T00:00:00Z"))

    sets_for_a = store.list_for_run(ctx, "run-A")
    sets_for_b = store.list_for_run(ctx, "run-B")
    sets_for_unknown = store.list_for_run(ctx, "run-X")

    assert [v.validation_set_id for v in sets_for_a] == ["vs-A2", "vs-A1"]
    assert [v.validation_set_id for v in sets_for_b] == ["vs-B1"]
    assert sets_for_unknown == []


def test_set_store_cross_tenant_isolation(workspace, ctx):
    """A set written under (acme, alpha) is invisible from
    (acme, beta) or (enemy, alpha). This is the core ownership
    guarantee the REST layer leans on."""
    store = JsonlValidationSetStore(workspace)
    store.upsert(ctx, _make_set(set_id="vs-1", run_id="run-1"))

    other_project = ProjectContext(tenant_id=ctx.tenant_id, project_id="beta")
    other_tenant = ProjectContext(tenant_id="enemy", project_id=ctx.project_id)

    assert store.get(other_project, "vs-1") is None
    assert store.get(other_tenant, "vs-1") is None
    assert store.list_for_run(other_project, "run-1") == []
    assert store.list_for_run(other_tenant, "run-1") == []


def test_set_store_tolerates_malformed_jsonl_line(workspace, ctx):
    """A truncated tail line (e.g. process killed mid-write) must
    not poison reads of every other set in the file."""
    store = JsonlValidationSetStore(workspace)
    store.upsert(ctx, _make_set(set_id="vs-good"))

    path = workspace.validation(ctx) / "validation_sets.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")

    fetched = store.get(ctx, "vs-good")
    assert fetched is not None  # malformed line skipped


# ---- ValidationRunStore --------------------------------------------


def test_run_store_round_trips_results_and_summary(workspace, ctx):
    """The validation run carries the most nested DTO graph in this
    surface. Round-trip locks every layer (chunk refs, citations,
    checks, summary, coverage) at once."""
    store = JsonlValidationRunStore(workspace)
    result = ValidationResultDTO(
        result_id="vr-001",
        test_case_id="tc-1",
        status="passed",
        question="What is X?",
        answer="X is …",
        retrieved_chunks=[
            RetrievedChunkRefDTO(
                artifact_id="a-1",
                chunk_id="c-1",
                run_id="run-1",
                document_id="doc-1",
                source_location="p.1",
                score=0.87,
                preview="…",
            ),
        ],
        citations=[
            ValidationCitationDTO(
                artifact_id="a-1",
                artifact_type="chunk",
                source_document_id="doc-1",
                source_location="p.1",
                chunk_id="c-1",
                run_id="run-1",
            ),
        ],
        checks=[
            ValidationCheckDTO(
                name="answer_non_empty", severity="required", passed=True,
            ),
        ],
    )
    summary = ValidationSummaryDTO(
        total=1, passed=1, warning=0, failed=0, skipped=0,
        coverage=ValidationCoverageDTO(by_type={"retrieval": 1}),
        main_issues=[],
        recommended_action="ready",
    )
    vrun = _make_run(results=[result], summary=summary)
    store.upsert(ctx, vrun)

    fetched = store.get(ctx, "vrun-1")
    assert fetched is not None
    assert fetched.execution_status == "completed"
    assert fetched.validation_status == "passed"
    assert len(fetched.results) == 1
    r = fetched.results[0]
    assert r.result_id == "vr-001"
    assert r.retrieved_chunks[0].chunk_id == "c-1"
    assert r.citations[0].run_id == "run-1"
    assert r.checks[0].passed is True
    assert fetched.summary.total == 1
    assert fetched.summary.coverage.by_type == {"retrieval": 1}
    assert fetched.summary.recommended_action == "ready"


def test_run_store_lifecycle_upserts_latest_wins(workspace, ctx):
    """Validation runs upsert at three lifecycle points: pending,
    running, terminal. The latest snapshot wins so callers always
    see the current state without having to reduce a journal."""
    store = JsonlValidationRunStore(workspace)
    store.upsert(ctx, _make_run(execution_status="pending", validation_status="inconclusive"))
    store.upsert(ctx, _make_run(execution_status="running", validation_status="inconclusive"))
    store.upsert(ctx, _make_run(execution_status="completed", validation_status="passed"))

    fetched = store.get(ctx, "vrun-1")
    assert fetched is not None
    assert fetched.execution_status == "completed"
    assert fetched.validation_status == "passed"


def test_run_store_status_split_persists(workspace, ctx):
    """The execution / validation status fields are independent —
    `completed` + `failed` is a real and supported state. The
    store must not collapse them on serialise/deserialise."""
    store = JsonlValidationRunStore(workspace)
    store.upsert(
        ctx,
        _make_run(execution_status="completed", validation_status="failed"),
    )

    fetched = store.get(ctx, "vrun-1")
    assert fetched is not None
    assert fetched.execution_status == "completed"
    assert fetched.validation_status == "failed"


def test_run_store_list_for_run_filters_and_orders(workspace, ctx):
    store = JsonlValidationRunStore(workspace)
    store.upsert(ctx, _make_run(vrun_id="vr-1", run_id="run-A", started_at="2026-01-01T00:00:00Z"))
    store.upsert(ctx, _make_run(vrun_id="vr-2", run_id="run-A", started_at="2026-05-07T10:00:00Z"))
    store.upsert(ctx, _make_run(vrun_id="vr-3", run_id="run-B"))

    a = store.list_for_run(ctx, "run-A")
    b = store.list_for_run(ctx, "run-B")
    assert [v.validation_run_id for v in a] == ["vr-2", "vr-1"]
    assert [v.validation_run_id for v in b] == ["vr-3"]


def test_run_store_cross_tenant_isolation(workspace, ctx):
    store = JsonlValidationRunStore(workspace)
    store.upsert(ctx, _make_run(vrun_id="vr-1", run_id="run-1"))

    other_project = ProjectContext(tenant_id=ctx.tenant_id, project_id="beta")
    assert store.get(other_project, "vr-1") is None
    assert store.list_for_run(other_project, "run-1") == []


def test_run_store_tolerates_malformed_jsonl_line(workspace, ctx):
    store = JsonlValidationRunStore(workspace)
    store.upsert(ctx, _make_run(vrun_id="vr-good"))

    path = workspace.validation(ctx) / "validation_runs.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{half-written\n")

    assert store.get(ctx, "vr-good") is not None
