"""Imported test cases — the auxiliary validation helper.

After the 2026-05-14 product decision, generated test cases are gone.
Imported test cases are an *auxiliary* surface inside the Validation
Tab — users upload a CSV per document and run it against the active
run for a quick confidence summary. There is no draft/approve/archive
lifecycle, no LLM question generation, no answer judging.

This module owns:

* The dataclasses for one imported case, one imported set, and one
  execution snapshot (per-question status + overall summary).
* The CSV importer: a small permissive parser keyed on the
  ``question`` column with optional ``expected_answer``,
  ``expected_sources``, ``test_type``, ``notes`` columns.
* The per-document store — a single JSONL file at
  ``{workspace}/runtime/imported_test_cases/{document_id}.jsonl``.
  Every import replaces the file entirely so the prior set is gone.
* The executor — runs each question through the SmartQueryOrchestrator
  scoped to the document's latest succeeded run, captures only the
  signals the UI needs (answer present, sources present, scope ok,
  error) and computes summary counts + overall status.

Status vocabulary (per question):

* ``not_run``       — imported, never executed.
* ``answered``      — orchestrator returned a non-empty answer.
* ``no_answer``     — orchestrator returned an empty / refusal answer.
* ``no_sources``    — answered but no citations / evidence chunks.
* ``scope_error``   — sources came from outside the active run.
* ``error``         — query path raised.

Overall status (rolled up across questions):

* ``good``          — most answered, most have sources, no scope issue.
* ``needs_review``  — some failures or missing sources, no severe issue.
* ``poor``          — many failures, many lack sources, or any scope issue.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from j1._serialization import to_jsonable
from j1.memory import MemoryNotQueryableError
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.validation.imported")


# ---- Public type vocabulary --------------------------------------

ImportedTestCaseStatus = Literal[
    "not_run", "answered", "no_answer", "no_sources",
    "scope_error", "error",
]

OverallStatus = Literal["good", "needs_review", "poor"]


# ---- Dataclasses -------------------------------------------------


@dataclass(frozen=True)
class ImportedTestCase:
    """One question parsed from a CSV row.

    Identity is the row's slot in the imported set; ``test_case_id``
    is server-allocated so the executor and the UI can address the
    case without relying on row order.
    """

    test_case_id: str
    question: str
    expected_answer: str | None = None
    expected_sources: tuple[str, ...] = ()
    test_type: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ImportedTestCaseSet:
    """The current imported set for one document.

    Replaces the previous set on every import — a project never has
    two imported sets for the same document at once."""

    document_id: str
    cases: tuple[ImportedTestCase, ...]
    imported_at: datetime
    source_filename: str | None = None


@dataclass(frozen=True)
class ImportedTestCaseResult:
    """One executed question's outcome."""

    test_case_id: str
    question: str
    status: ImportedTestCaseStatus
    has_sources: bool
    scope_ok: bool
    error: str | None = None
    # The run this question was executed against. Recorded so the FE
    # can show "results computed against run X" without inferring.
    run_id: str | None = None


@dataclass(frozen=True)
class ImportedTestCaseSummary:
    """Aggregate counts + overall verdict."""

    total: int
    answered: int
    with_sources: int
    scope_issues: int
    errors: int
    overall: OverallStatus


@dataclass(frozen=True)
class ImportedTestCaseExecution:
    """One snapshot of executing the imported set.

    The store keeps the *latest* execution only — the UI shows quick
    confidence, not history. If the user wants per-question detail,
    they open the question in Manual Test Query."""

    document_id: str
    executed_at: datetime
    run_id: str | None
    results: tuple[ImportedTestCaseResult, ...]
    summary: ImportedTestCaseSummary


# ---- CSV importer ------------------------------------------------


class CSVImportError(Exception):
    """Raised when the CSV is unreadable or missing the required column."""


_REQUIRED_COLUMN = "question"
_OPTIONAL_COLUMNS = frozenset({
    "expected_answer", "expected_sources", "test_type", "notes",
})

# Heuristic: ``expected_sources`` can be a single source or a
# comma/semicolon/pipe-separated list. We split permissively and
# strip blanks so users can paste lists in any common shape.
_EXPECTED_SOURCES_SPLIT = (",", ";", "|", "\n")


def parse_csv_bytes(
    raw: bytes,
    *,
    source_filename: str | None = None,
    encoding: str = "utf-8-sig",
) -> tuple[ImportedTestCase, ...]:
    """Parse a CSV blob into ``ImportedTestCase`` rows.

    Tolerant of UTF-8 BOMs (``utf-8-sig``), CRLF line endings, blank
    lines, and trailing whitespace. Column lookup is case-insensitive
    on header names. Rows with an empty ``question`` cell are skipped
    rather than raised: spreadsheet exports routinely carry a trailing
    blank row.

    Raises ``CSVImportError`` only for unrecoverable problems — bad
    encoding, no header row, or missing ``question`` column entirely.
    """
    if not raw:
        raise CSVImportError("empty file")
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise CSVImportError(
            f"failed to decode CSV as {encoding}: {exc}"
        ) from exc

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CSVImportError("CSV has no header row")

    # Build a case-insensitive header → canonical-name map.
    header_lookup: dict[str, str] = {}
    for col in reader.fieldnames:
        if col is None:
            continue
        key = col.strip().lower()
        if not key:
            continue
        if key == _REQUIRED_COLUMN or key in _OPTIONAL_COLUMNS:
            header_lookup[key] = col

    if _REQUIRED_COLUMN not in header_lookup:
        raise CSVImportError(
            f"CSV missing required column '{_REQUIRED_COLUMN}'. "
            f"Found: {list(reader.fieldnames)}"
        )

    cases: list[ImportedTestCase] = []
    for row in reader:
        question = (row.get(header_lookup[_REQUIRED_COLUMN]) or "").strip()
        if not question:
            continue
        expected_answer = _opt(row, header_lookup, "expected_answer")
        expected_sources_raw = _opt(row, header_lookup, "expected_sources")
        test_type = _opt(row, header_lookup, "test_type")
        notes = _opt(row, header_lookup, "notes")
        cases.append(ImportedTestCase(
            test_case_id=f"itc-{uuid.uuid4().hex[:12]}",
            question=question,
            expected_answer=expected_answer,
            expected_sources=_split_sources(expected_sources_raw),
            test_type=test_type,
            notes=notes,
        ))
    return tuple(cases)


def _opt(
    row: dict[str, str],
    header_lookup: dict[str, str],
    key: str,
) -> str | None:
    col = header_lookup.get(key)
    if col is None:
        return None
    raw = row.get(col)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _split_sources(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    pieces: list[str] = [raw]
    for sep in _EXPECTED_SOURCES_SPLIT:
        next_pieces: list[str] = []
        for piece in pieces:
            next_pieces.extend(piece.split(sep))
        pieces = next_pieces
    return tuple(
        cleaned for cleaned in (p.strip() for p in pieces) if cleaned
    )


# ---- Store -------------------------------------------------------


_IMPORTED_DIR = "imported_test_cases"


class ImportedTestCaseStore(Protocol):
    """Per-document imported set + latest execution snapshot."""

    def save_set(
        self, ctx: ProjectContext, imported_set: ImportedTestCaseSet,
    ) -> None: ...

    def get_set(
        self, ctx: ProjectContext, document_id: str,
    ) -> ImportedTestCaseSet | None: ...

    def delete_set(
        self, ctx: ProjectContext, document_id: str,
    ) -> bool: ...

    def save_execution(
        self,
        ctx: ProjectContext,
        execution: ImportedTestCaseExecution,
    ) -> None: ...

    def get_latest_execution(
        self, ctx: ProjectContext, document_id: str,
    ) -> ImportedTestCaseExecution | None: ...


class JsonlImportedTestCaseStore:
    """One JSONL file per document. First line is the set, last
    non-set line is the latest execution snapshot.

    Layout: ``{workspace.runtime(ctx)}/imported_test_cases/{document_id}.jsonl``.
    Every ``save_set`` rewrites the file atomically so the prior
    import is gone — matches the product spec exactly.
    """

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    # ---- Writes --------------------------------------------------

    def save_set(
        self, ctx: ProjectContext, imported_set: ImportedTestCaseSet,
    ) -> None:
        path = self._path(ctx, imported_set.document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replace semantics: every import wipes the prior set AND
        # the prior execution snapshot. Stale executions are
        # meaningless once the questions change.
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"kind": "set", **to_jsonable(imported_set)},
                separators=(",", ":"),
            ))
            fh.write("\n")
        tmp.replace(path)

    def delete_set(
        self, ctx: ProjectContext, document_id: str,
    ) -> bool:
        path = self._path(ctx, document_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def save_execution(
        self,
        ctx: ProjectContext,
        execution: ImportedTestCaseExecution,
    ) -> None:
        # Read the existing set so we can preserve it alongside the
        # new execution snapshot. The store keeps EXACTLY one
        # execution at a time — re-running replaces the prior
        # snapshot, matching the product spec's "compact summary"
        # framing.
        existing = self.get_set(ctx, execution.document_id)
        path = self._path(ctx, execution.document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            if existing is not None:
                fh.write(json.dumps(
                    {"kind": "set", **to_jsonable(existing)},
                    separators=(",", ":"),
                ))
                fh.write("\n")
            fh.write(json.dumps(
                {"kind": "execution", **to_jsonable(execution)},
                separators=(",", ":"),
            ))
            fh.write("\n")
        tmp.replace(path)

    # ---- Reads ---------------------------------------------------

    def get_set(
        self, ctx: ProjectContext, document_id: str,
    ) -> ImportedTestCaseSet | None:
        for record in self._iter_records(ctx, document_id):
            if record.get("kind") == "set":
                return _set_from_dict(record)
        return None

    def get_latest_execution(
        self, ctx: ProjectContext, document_id: str,
    ) -> ImportedTestCaseExecution | None:
        latest: dict[str, Any] | None = None
        for record in self._iter_records(ctx, document_id):
            if record.get("kind") == "execution":
                latest = record
        if latest is None:
            return None
        return _execution_from_dict(latest)

    # ---- Internals -----------------------------------------------

    def _path(self, ctx: ProjectContext, document_id: str) -> Path:
        return (
            self._workspace.runtime(ctx)
            / _IMPORTED_DIR
            / f"{document_id}.jsonl"
        )

    def _iter_records(
        self, ctx: ProjectContext, document_id: str,
    ) -> Iterable[dict[str, Any]]:
        path = self._path(ctx, document_id)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    _log.warning(
                        "imported_test_cases: skipping malformed "
                        "line in %s", path,
                    )


# ---- Deserialisation helpers -------------------------------------


def _set_from_dict(d: dict[str, Any]) -> ImportedTestCaseSet:
    cases = tuple(
        ImportedTestCase(
            test_case_id=c["test_case_id"],
            question=c["question"],
            expected_answer=c.get("expected_answer"),
            expected_sources=tuple(c.get("expected_sources") or ()),
            test_type=c.get("test_type"),
            notes=c.get("notes"),
        )
        for c in d.get("cases", [])
    )
    return ImportedTestCaseSet(
        document_id=d["document_id"],
        cases=cases,
        imported_at=datetime.fromisoformat(d["imported_at"]),
        source_filename=d.get("source_filename"),
    )


def _execution_from_dict(d: dict[str, Any]) -> ImportedTestCaseExecution:
    results = tuple(
        ImportedTestCaseResult(
            test_case_id=r["test_case_id"],
            question=r["question"],
            status=r["status"],
            has_sources=bool(r.get("has_sources")),
            scope_ok=bool(r.get("scope_ok")),
            error=r.get("error"),
            run_id=r.get("run_id"),
        )
        for r in d.get("results", [])
    )
    s = d.get("summary") or {}
    summary = ImportedTestCaseSummary(
        total=int(s.get("total") or 0),
        answered=int(s.get("answered") or 0),
        with_sources=int(s.get("with_sources") or 0),
        scope_issues=int(s.get("scope_issues") or 0),
        errors=int(s.get("errors") or 0),
        overall=s.get("overall") or "needs_review",
    )
    return ImportedTestCaseExecution(
        document_id=d["document_id"],
        executed_at=datetime.fromisoformat(d["executed_at"]),
        run_id=d.get("run_id"),
        results=results,
        summary=summary,
    )


# ---- Summary computation -----------------------------------------


def compute_summary(
    results: Iterable[ImportedTestCaseResult],
) -> ImportedTestCaseSummary:
    """Roll a sequence of per-question results into the UI summary.

    Thresholds are intentionally simple — this is a quick confidence
    surface, not a scoring system:

    * ``good``          — every executed question answered, every
                          answered one has sources, no scope issue
                          and no errors.
    * ``poor``          — any scope issue, or majority unanswered, or
                          majority answered without sources.
    * ``needs_review``  — everything else.
    """
    total = 0
    answered = 0
    with_sources = 0
    scope_issues = 0
    errors = 0
    not_run = 0
    for r in results:
        total += 1
        if r.status == "answered":
            answered += 1
            if r.has_sources:
                with_sources += 1
        elif r.status == "no_sources":
            answered += 1  # answered, just no sources cited
        elif r.status == "scope_error":
            answered += 1
            scope_issues += 1
        elif r.status == "error":
            errors += 1
        elif r.status == "not_run":
            not_run += 1
        # no_answer falls through — counted in `total` but not
        # `answered`.

    overall = _compute_overall(
        total=total,
        answered=answered,
        with_sources=with_sources,
        scope_issues=scope_issues,
        errors=errors,
        not_run=not_run,
    )
    return ImportedTestCaseSummary(
        total=total,
        answered=answered,
        with_sources=with_sources,
        scope_issues=scope_issues,
        errors=errors,
        overall=overall,
    )


def _compute_overall(
    *,
    total: int,
    answered: int,
    with_sources: int,
    scope_issues: int,
    errors: int,
    not_run: int,
) -> OverallStatus:
    if total == 0:
        return "needs_review"
    # Any scope issue = poor. Scope problems mean the retrieval path
    # is grabbing the wrong document's evidence; that's a serious
    # signal regardless of how the other questions did.
    if scope_issues > 0:
        return "poor"
    # If most questions are unanswered or most answers lack sources,
    # the import is poor. Threshold = strictly more than half.
    executed = total - not_run
    if executed == 0:
        return "needs_review"
    unanswered = executed - answered
    answered_without_sources = answered - with_sources
    if unanswered * 2 > executed:
        return "poor"
    if answered_without_sources * 2 > answered and answered > 0:
        return "poor"
    if errors > 0:
        return "needs_review"
    if unanswered > 0 or answered_without_sources > 0:
        return "needs_review"
    return "good"


# ---- Executor ----------------------------------------------------


def _is_empty_answer(answer: str | None) -> bool:
    if answer is None:
        return True
    cleaned = answer.strip()
    if not cleaned:
        return True
    # Common refusal phrasings. We deliberately keep this list short
    # and obvious — the orchestrator's quality gate already classifies
    # refusals upstream; this is a belt-and-braces check for tests
    # that bypass the gate.
    refusal_markers = (
        "i don't know",
        "i do not know",
        "no answer available",
        "cannot answer",
    )
    lowered = cleaned.lower()
    return any(lowered.startswith(m) for m in refusal_markers)


@dataclass
class ImportedTestCaseExecutor:
    """Runs an imported set through the SmartQueryOrchestrator.

    Stateless aside from its injected collaborators. One instance per
    deployment is fine — every run takes a fresh ``ctx`` and reads
    the active run from the run store.
    """

    smart_query_orchestrator: Any
    run_store: Any  # IngestionRunStore — not typed to avoid cycles
    clock: Any = None

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        return datetime.now(timezone.utc)

    def execute(
        self,
        ctx: ProjectContext,
        imported_set: ImportedTestCaseSet,
        *,
        run_id: str,
    ) -> ImportedTestCaseExecution:
        """Run every question through the orchestrator scoped to
        ``run_id`` and return the execution snapshot.

        ``run_id`` is the latest succeeded run for the document —
        the caller picks it via ``_latest_succeeded_run_id`` (the
        same heuristic the REST reindex flow uses).
        """
        from j1.query.scope import RunScope
        from j1.query.orchestrator import OrchestratorRequest

        document_id = imported_set.document_id
        results: list[ImportedTestCaseResult] = []
        for case in imported_set.cases:
            results.append(self._execute_one(
                ctx=ctx,
                case=case,
                document_id=document_id,
                run_id=run_id,
                RunScope=RunScope,
                OrchestratorRequest=OrchestratorRequest,
            ))
        summary = compute_summary(results)
        return ImportedTestCaseExecution(
            document_id=document_id,
            executed_at=self._now(),
            run_id=run_id,
            results=tuple(results),
            summary=summary,
        )

    def _execute_one(
        self,
        *,
        ctx: ProjectContext,
        case: ImportedTestCase,
        document_id: str,
        run_id: str,
        RunScope,
        OrchestratorRequest,
    ) -> ImportedTestCaseResult:
        try:
            result = self.smart_query_orchestrator.run(OrchestratorRequest(
                ctx=ctx,
                question=case.question,
                scope=RunScope(run_id=run_id),
                run_id=run_id,
                document_id=document_id,
            ))
        # Re-raise the Unified Memory queryability refusal verbatim.
        # The CSV runner intentionally captures per-question errors
        # into ``status="error"``, but ``MemoryNotQueryableError``
        # is a SCOPE-LEVEL refusal that applies to every question in
        # the batch — converting it into a per-question error would
        # silently produce a misleading summary. Let it bubble so
        # the REST handler converts the whole batch to HTTP 409.
        except MemoryNotQueryableError:
            raise
        except Exception as exc:  # noqa: BLE001 — capture, don't propagate
            _log.warning(
                "imported_test_case execution failed: %s", exc,
                exc_info=True,
            )
            return ImportedTestCaseResult(
                test_case_id=case.test_case_id,
                question=case.question,
                status="error",
                has_sources=False,
                scope_ok=True,
                error=f"{type(exc).__name__}: {exc}",
                run_id=run_id,
            )

        # Extract the signals the UI cares about. The orchestrator's
        # trace + result shape vary by deployment; we read defensively.
        answer = getattr(result, "answer", None)
        citations = getattr(result, "citations", None) or []
        trace = getattr(result, "trace", None)

        has_sources = bool(citations) or _trace_has_sources(trace)
        scope_ok = _trace_scope_ok(trace, expected_run_id=run_id)

        if _is_empty_answer(answer):
            status: ImportedTestCaseStatus = "no_answer"
        elif not scope_ok:
            status = "scope_error"
        elif not has_sources:
            status = "no_sources"
        else:
            status = "answered"

        return ImportedTestCaseResult(
            test_case_id=case.test_case_id,
            question=case.question,
            status=status,
            has_sources=has_sources,
            scope_ok=scope_ok,
            error=None,
            run_id=run_id,
        )


def _trace_has_sources(trace: Any) -> bool:
    if trace is None:
        return False
    # The orchestrator's trace exposes ``citations`` and/or
    # ``selected_evidence`` collections; either signals "answer is
    # grounded in retrieved evidence" for our purposes.
    for attr in ("citations", "selected_evidence", "evidence_groups"):
        val = getattr(trace, attr, None)
        if val:
            return True
    return False


def _trace_scope_ok(trace: Any, *, expected_run_id: str) -> bool:
    """True when every evidence chunk the orchestrator surfaced
    belongs to the expected run.

    Best-effort: when the trace doesn't expose source-of-each-chunk
    information, we default to True (no evidence of a leak) rather
    than fail the check. The orchestrator already enforces RunScope
    on retrieval, so leaks are unexpected — this is just a guard rail.
    """
    if trace is None:
        return True
    for attr in ("selected_evidence", "evidence_groups"):
        items = getattr(trace, attr, None) or ()
        for item in items:
            chunk_run_id = (
                getattr(item, "run_id", None)
                or _dict_get(item, "run_id")
            )
            if chunk_run_id and chunk_run_id != expected_run_id:
                return False
    return True


def _dict_get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return None


__all__ = [
    "CSVImportError",
    "ImportedTestCase",
    "ImportedTestCaseExecution",
    "ImportedTestCaseExecutor",
    "ImportedTestCaseResult",
    "ImportedTestCaseSet",
    "ImportedTestCaseStatus",
    "ImportedTestCaseStore",
    "ImportedTestCaseSummary",
    "JsonlImportedTestCaseStore",
    "OverallStatus",
    "compute_summary",
    "parse_csv_bytes",
]
