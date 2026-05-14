"""Tests for the auxiliary "Imported Test Cases" validation helper.

Generated test cases were deleted in the 2026-05-14 product change.
This file covers the only remaining surface besides Manual Test
Query: a user uploads a CSV per document, executes it against the
document's latest succeeded run, and gets back a compact summary +
per-question status.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from j1.validation.imported_test_cases import (
    CSVImportError,
    ImportedTestCase,
    ImportedTestCaseExecutor,
    ImportedTestCaseResult,
    ImportedTestCaseSet,
    JsonlImportedTestCaseStore,
    compute_summary,
    parse_csv_bytes,
)


# ---- CSV parsing -------------------------------------------------


def test_parse_csv_minimum_required_column_only():
    cases = parse_csv_bytes(b"question\nWhat is X?\nWhen is Y?\n")
    assert len(cases) == 2
    assert cases[0].question == "What is X?"
    assert cases[1].question == "When is Y?"
    assert cases[0].expected_answer is None
    assert cases[0].expected_sources == ()
    assert cases[0].test_case_id.startswith("itc-")


def test_parse_csv_with_all_optional_columns():
    raw = (
        b"question,expected_answer,expected_sources,test_type,notes\n"
        b"What?,The answer,doc-1;doc-2,fact_check,manual\n"
    )
    cases = parse_csv_bytes(raw)
    assert len(cases) == 1
    case = cases[0]
    assert case.question == "What?"
    assert case.expected_answer == "The answer"
    assert case.expected_sources == ("doc-1", "doc-2")
    assert case.test_type == "fact_check"
    assert case.notes == "manual"


def test_parse_csv_tolerates_utf8_bom_and_blank_rows():
    # UTF-8 BOM at the start + a blank trailing row.
    raw = "﻿question\nFoo\n\n".encode("utf-8")
    cases = parse_csv_bytes(raw)
    assert len(cases) == 1
    assert cases[0].question == "Foo"


def test_parse_csv_is_case_insensitive_on_headers():
    raw = b"QUESTION,Expected_Answer\nq,a\n"
    cases = parse_csv_bytes(raw)
    assert len(cases) == 1
    assert cases[0].expected_answer == "a"


def test_parse_csv_splits_expected_sources_on_common_delimiters():
    # Commas inside a CSV cell must be quoted; semicolons and pipes
    # don't need quoting because they aren't the CSV field delimiter.
    cases = []
    for sep, rendered in (
        (",", '"a,b,c"'),
        (";", "a;b;c"),
        ("|", "a|b|c"),
    ):
        raw = f"question,expected_sources\nq,{rendered}\n".encode("utf-8")
        parsed = parse_csv_bytes(raw)
        cases.append((sep, parsed[0].expected_sources))
    for sep, sources in cases:
        assert sources == ("a", "b", "c"), sep


def test_parse_csv_missing_required_column_raises():
    with pytest.raises(CSVImportError, match="missing required column"):
        parse_csv_bytes(b"something_else\nfoo\n")


def test_parse_csv_empty_file_raises():
    with pytest.raises(CSVImportError, match="empty file"):
        parse_csv_bytes(b"")


def test_parse_csv_no_header_raises():
    # csv.DictReader needs at least one row to derive the header.
    # An empty-after-decode payload still passes the upfront check —
    # but a payload that decodes to an empty body should be flagged.
    # Use a bytes-only-whitespace input.
    with pytest.raises(CSVImportError):
        parse_csv_bytes(b"\n")


# ---- Summary computation -----------------------------------------


def _result(test_case_id: str, status: str, *, has_sources: bool = True,
            scope_ok: bool = True) -> ImportedTestCaseResult:
    return ImportedTestCaseResult(
        test_case_id=test_case_id,
        question=f"q-{test_case_id}",
        status=status,  # type: ignore[arg-type]
        has_sources=has_sources,
        scope_ok=scope_ok,
        run_id="r",
    )


def test_compute_summary_returns_needs_review_when_empty():
    out = compute_summary([])
    assert out.total == 0
    assert out.overall == "needs_review"


def test_compute_summary_good_when_everything_answered_with_sources():
    out = compute_summary([
        _result("1", "answered"),
        _result("2", "answered"),
    ])
    assert out.overall == "good"
    assert out.total == 2
    assert out.answered == 2
    assert out.with_sources == 2


def test_compute_summary_needs_review_when_some_lack_sources():
    out = compute_summary([
        _result("1", "answered"),
        _result("2", "no_sources", has_sources=False),
    ])
    assert out.overall == "needs_review"
    assert out.answered == 2
    assert out.with_sources == 1


def test_compute_summary_poor_when_majority_unanswered():
    out = compute_summary([
        _result("1", "no_answer", has_sources=False),
        _result("2", "no_answer", has_sources=False),
        _result("3", "answered"),
    ])
    assert out.overall == "poor"


def test_compute_summary_poor_on_any_scope_issue():
    out = compute_summary([
        _result("1", "answered"),
        _result("2", "answered"),
        _result("3", "scope_error", scope_ok=False),
    ])
    assert out.overall == "poor"
    assert out.scope_issues == 1


def test_compute_summary_treats_errors_as_needs_review_when_minor():
    out = compute_summary([
        _result("1", "answered"),
        _result("2", "answered"),
        _result("3", "error", has_sources=False),
    ])
    assert out.overall == "needs_review"
    assert out.errors == 1


# ---- Store -------------------------------------------------------


def _set(workspace, ctx, *questions: str) -> ImportedTestCaseSet:
    return ImportedTestCaseSet(
        document_id="doc-1",
        cases=tuple(
            ImportedTestCase(
                test_case_id=f"itc-{i}", question=q,
            )
            for i, q in enumerate(questions)
        ),
        imported_at=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        source_filename="tests.csv",
    )


def test_store_save_and_get_set_round_trips(workspace, ctx):
    store = JsonlImportedTestCaseStore(workspace)
    imported = _set(workspace, ctx, "What is X?")
    store.save_set(ctx, imported)
    out = store.get_set(ctx, "doc-1")
    assert out is not None
    assert out.document_id == "doc-1"
    assert len(out.cases) == 1
    assert out.cases[0].question == "What is X?"


def test_store_replace_semantics_wipes_prior_set(workspace, ctx):
    store = JsonlImportedTestCaseStore(workspace)
    store.save_set(ctx, _set(workspace, ctx, "Q1", "Q2", "Q3"))
    # Re-import with a single different question — prior set MUST be
    # gone.
    store.save_set(ctx, _set(workspace, ctx, "OnlyOne"))
    out = store.get_set(ctx, "doc-1")
    assert out is not None
    assert [c.question for c in out.cases] == ["OnlyOne"]


def test_store_save_execution_keeps_only_one_snapshot(workspace, ctx):
    store = JsonlImportedTestCaseStore(workspace)
    store.save_set(ctx, _set(workspace, ctx, "Q1"))
    from j1.validation.imported_test_cases import (
        ImportedTestCaseExecution,
        ImportedTestCaseSummary,
    )
    summary = ImportedTestCaseSummary(
        total=1, answered=1, with_sources=1, scope_issues=0,
        errors=0, overall="good",
    )
    exec_a = ImportedTestCaseExecution(
        document_id="doc-1",
        executed_at=datetime(2026, 5, 14, 11, tzinfo=timezone.utc),
        run_id="run-a",
        results=(_result("itc-0", "answered"),),
        summary=summary,
    )
    store.save_execution(ctx, exec_a)
    # A second execution should overwrite the prior snapshot —
    # the store keeps the LATEST only.
    exec_b = ImportedTestCaseExecution(
        document_id="doc-1",
        executed_at=exec_a.executed_at + timedelta(hours=1),
        run_id="run-b",
        results=(_result("itc-0", "no_answer", has_sources=False),),
        summary=summary,
    )
    store.save_execution(ctx, exec_b)
    out = store.get_latest_execution(ctx, "doc-1")
    assert out is not None
    assert out.run_id == "run-b"
    assert out.results[0].status == "no_answer"


def test_store_save_set_wipes_prior_execution(workspace, ctx):
    """Per the product spec: every import must drop the prior
    execution snapshot — stale results never linger on a refreshed
    question list."""
    store = JsonlImportedTestCaseStore(workspace)
    store.save_set(ctx, _set(workspace, ctx, "Q1"))
    from j1.validation.imported_test_cases import (
        ImportedTestCaseExecution,
        ImportedTestCaseSummary,
    )
    store.save_execution(ctx, ImportedTestCaseExecution(
        document_id="doc-1",
        executed_at=datetime(2026, 5, 14, 11, tzinfo=timezone.utc),
        run_id="r",
        results=(_result("itc-0", "answered"),),
        summary=ImportedTestCaseSummary(
            total=1, answered=1, with_sources=1, scope_issues=0,
            errors=0, overall="good",
        ),
    ))
    assert store.get_latest_execution(ctx, "doc-1") is not None
    # New import wipes everything.
    store.save_set(ctx, _set(workspace, ctx, "Q-new"))
    assert store.get_latest_execution(ctx, "doc-1") is None


def test_store_delete_set_removes_file(workspace, ctx):
    store = JsonlImportedTestCaseStore(workspace)
    store.save_set(ctx, _set(workspace, ctx, "Q1"))
    assert store.delete_set(ctx, "doc-1") is True
    assert store.get_set(ctx, "doc-1") is None
    # Idempotent: second delete returns False but doesn't raise.
    assert store.delete_set(ctx, "doc-1") is False


# ---- Executor ----------------------------------------------------


class _FakeOrchestrator:
    """Captures every ``run()`` call and returns scripted results."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def run(self, request):
        self.calls.append(request)
        return self._results.pop(0)


def _orchestrator_result(
    *, answer: str | None, citations=(), evidence=(), raises: bool = False,
):
    """Build a SimpleNamespace shaped like the orchestrator's result.

    The executor reads ``answer``, ``citations``, and ``trace`` (with
    ``selected_evidence``/``evidence_groups`` / ``citations`` sub-
    collections). Anything else is irrelevant for our signal extraction.
    """
    if raises:
        return None  # never used — caller wires a side effect instead
    trace = SimpleNamespace(
        selected_evidence=tuple(evidence),
        evidence_groups=(),
        citations=tuple(citations),
    )
    return SimpleNamespace(
        answer=answer,
        citations=tuple(citations),
        trace=trace,
    )


def _executor(orch) -> ImportedTestCaseExecutor:
    return ImportedTestCaseExecutor(
        smart_query_orchestrator=orch,
        run_store=None,  # not consulted by execute()
    )


def _imported_set(*questions: str) -> ImportedTestCaseSet:
    return ImportedTestCaseSet(
        document_id="doc-1",
        cases=tuple(
            ImportedTestCase(test_case_id=f"itc-{i}", question=q)
            for i, q in enumerate(questions)
        ),
        imported_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
    )


def test_executor_marks_answered_when_orchestrator_returns_answer_and_sources(
    ctx,
):
    orch = _FakeOrchestrator([
        _orchestrator_result(
            answer="It is X.",
            citations=[SimpleNamespace(
                candidate=SimpleNamespace(
                    artifact_id="a", chunk_id=None, document_id="doc-1",
                    run_id="run-1", artifact_kind="chunk", extra={},
                    score=0.9,
                ),
            )],
        ),
    ])
    execution = _executor(orch).execute(
        ctx, _imported_set("Q1"), run_id="run-1",
    )
    assert len(execution.results) == 1
    result = execution.results[0]
    assert result.status == "answered"
    assert result.has_sources is True
    assert result.scope_ok is True
    assert result.error is None
    assert execution.summary.overall == "good"
    # Orchestrator received the right run scope.
    assert orch.calls[0].run_id == "run-1"


def test_executor_marks_no_answer_when_orchestrator_returns_empty(ctx):
    orch = _FakeOrchestrator([
        _orchestrator_result(answer="", citations=[]),
    ])
    execution = _executor(orch).execute(
        ctx, _imported_set("Q1"), run_id="run-1",
    )
    assert execution.results[0].status == "no_answer"
    assert execution.summary.answered == 0


def test_executor_marks_no_sources_when_answer_but_no_citations(ctx):
    orch = _FakeOrchestrator([
        _orchestrator_result(answer="An answer.", citations=[]),
    ])
    execution = _executor(orch).execute(
        ctx, _imported_set("Q1"), run_id="run-1",
    )
    assert execution.results[0].status == "no_sources"


def test_executor_marks_scope_error_when_evidence_run_id_differs(ctx):
    # Trace surfaces a chunk whose run_id != the expected run.
    leak = SimpleNamespace(
        run_id="other-run", artifact_id="a", chunk_id=None,
    )
    orch = _FakeOrchestrator([
        _orchestrator_result(
            answer="An answer.",
            citations=[],
            evidence=[leak],
        ),
    ])
    execution = _executor(orch).execute(
        ctx, _imported_set("Q1"), run_id="run-1",
    )
    assert execution.results[0].status == "scope_error"
    assert execution.results[0].scope_ok is False
    assert execution.summary.overall == "poor"


def test_executor_captures_error_when_orchestrator_raises(ctx):
    class _Boom:
        def run(self, _):
            raise RuntimeError("kaboom")

    execution = _executor(_Boom()).execute(
        ctx, _imported_set("Q1"), run_id="run-1",
    )
    assert execution.results[0].status == "error"
    assert "kaboom" in (execution.results[0].error or "")
    assert execution.summary.errors == 1


# ---- Service-level smoke -----------------------------------------


def _build_service(workspace, *, orchestrator=None, run_store=None):
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.artifacts.registry import JsonArtifactRegistry
    from j1.runs.store import JsonlIngestionRunStore
    from j1.validation import (
        ImportedTestCaseExecutor,
        IngestionValidationService,
        JsonlImportedTestCaseStore,
    )
    rs = run_store or JsonlIngestionRunStore(workspace)
    store = JsonlImportedTestCaseStore(workspace)
    executor = ImportedTestCaseExecutor(
        smart_query_orchestrator=orchestrator,
        run_store=rs,
    ) if orchestrator is not None else None
    return IngestionValidationService(
        run_store=rs,
        artifact_registry=JsonArtifactRegistry(workspace),
        audit=DefaultAuditRecorder(JsonlAuditSink(workspace)),
        workspace=workspace,
        imported_test_case_store=store,
        imported_test_case_executor=executor,
        smart_query_orchestrator=orchestrator,
    )


def test_service_import_then_get_returns_the_set(workspace, ctx):
    service = _build_service(workspace)
    imported = service.import_test_cases(
        ctx, "doc-1",
        b"question\nWhat is X?\n",
        source_filename="tests.csv",
    )
    assert imported.document_id == "doc-1"
    assert len(imported.cases) == 1
    out = service.get_imported_test_cases(ctx, "doc-1")
    assert out is not None
    assert out.source_filename == "tests.csv"


def test_service_execute_requires_an_imported_set(workspace, ctx):
    from j1.ingestion_review.exceptions import ReviewNotFound
    service = _build_service(
        workspace, orchestrator=_FakeOrchestrator([]),
    )
    with pytest.raises(ReviewNotFound):
        service.execute_imported_test_cases(ctx, "doc-missing")


def test_service_execute_requires_a_succeeded_run(workspace, ctx):
    from j1.ingestion_review.exceptions import ReviewNotFound
    service = _build_service(
        workspace, orchestrator=_FakeOrchestrator([]),
    )
    service.import_test_cases(
        ctx, "doc-1", b"question\nQ\n", source_filename="t.csv",
    )
    with pytest.raises(ReviewNotFound):
        service.execute_imported_test_cases(ctx, "doc-1")


def test_service_execute_persists_an_execution_snapshot(workspace, ctx):
    from j1.runs.models import IngestionRun, RunStatus
    from j1.runs.store import JsonlIngestionRunStore
    run_store = JsonlIngestionRunStore(workspace)
    run_store.upsert(ctx, IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=datetime(2026, 5, 14, 9, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 14, 9, 5, tzinfo=timezone.utc),
    ))
    orch = _FakeOrchestrator([
        _orchestrator_result(
            answer="X.",
            citations=[SimpleNamespace(
                candidate=SimpleNamespace(
                    artifact_id="a", chunk_id=None, document_id="doc-1",
                    run_id="run-1", artifact_kind="chunk", extra={},
                    score=0.5,
                ),
            )],
        ),
    ])
    service = _build_service(workspace, orchestrator=orch, run_store=run_store)
    service.import_test_cases(
        ctx, "doc-1", b"question\nQ\n", source_filename="t.csv",
    )
    execution = service.execute_imported_test_cases(ctx, "doc-1")
    assert execution.run_id == "run-1"
    assert execution.summary.overall == "good"
    # Persisted: getter returns the same snapshot.
    persisted = service.get_latest_imported_execution(ctx, "doc-1")
    assert persisted is not None
    assert persisted.run_id == "run-1"
    assert persisted.summary.total == 1
