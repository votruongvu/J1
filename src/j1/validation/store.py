"""JSONL-backed stores for validation sets and validation runs.

Mirrors the `JsonlIngestionRunStore` pattern: append-only writes,
latest-snapshot-wins reads, scoped under the workspace's
`validation` area so the storage layout follows the same
tenant/project hierarchy the rest of the framework uses.

Two stores ship in Phase 2:

  * `JsonlValidationSetStore` — one record per generated set,
    upserted when generation completes (and on any future edit).
  * `JsonlValidationRunStore` — one record per run execution,
    upserted at three lifecycle points: pending → running →
    completed/failed/cancelled.

Records are stored as flat dicts via `_serialization.to_jsonable`,
hydrated back into typed dataclasses on read. Malformed lines are
skipped silently — the JSONL contract is best-effort, last-write-
wins, so a truncated tail line shouldn't poison the whole file.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Protocol

from j1._serialization import to_jsonable
from j1.projects.context import ProjectContext
from j1.validation.dtos import (
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationCoverageDTO,
    ValidationResultDTO,
    ValidationRunDTO,
    ValidationSetDTO,
    ValidationSummaryDTO,
    ValidationTestCaseDTO,
)
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

# Filenames sit under `validation/` (one subdir under the project
# root) so a single backup of the workspace also includes validation
# state. Two separate files keeps reads cheap — a project that has
# thousands of validation runs but only a few sets pays only the
# runs file's I/O when listing runs.
VALIDATION_SETS_FILENAME = "validation_sets.jsonl"
VALIDATION_RUNS_FILENAME = "validation_runs.jsonl"

__all__ = [
    "JsonlValidationRunStore",
    "JsonlValidationSetStore",
    "VALIDATION_RUNS_FILENAME",
    "VALIDATION_SETS_FILENAME",
    "ValidationRunStore",
    "ValidationSetStore",
]


# ---- Protocols (typing the read/write surface) ---------------------


class ValidationSetStore(Protocol):
    def upsert(self, ctx: ProjectContext, vset: ValidationSetDTO) -> None: ...

    def get(
        self, ctx: ProjectContext, validation_set_id: str,
    ) -> ValidationSetDTO | None: ...

    def list_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationSetDTO]: ...


class ValidationRunStore(Protocol):
    def upsert(self, ctx: ProjectContext, vrun: ValidationRunDTO) -> None: ...

    def get(
        self, ctx: ProjectContext, validation_run_id: str,
    ) -> ValidationRunDTO | None: ...

    def list_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationRunDTO]: ...


# ---- ValidationSetStore --------------------------------------------


class JsonlValidationSetStore:
    """Append-only JSONL set store. Latest snapshot wins per set id.

    Sets are typically written once at generation time and never
    edited (Phase 2). Phase 5's editing workflow will append revised
    snapshots; the latest-wins read makes that change a one-liner."""

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def _path(self, ctx: ProjectContext):
        return (
            self._workspace.area(ctx, WorkspaceArea.VALIDATION)
            / VALIDATION_SETS_FILENAME
        )

    def upsert(self, ctx: ProjectContext, vset: ValidationSetDTO) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(vset), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    def get(
        self, ctx: ProjectContext, validation_set_id: str,
    ) -> ValidationSetDTO | None:
        latest: ValidationSetDTO | None = None
        for vset in self._iter_all(ctx):
            if vset.validation_set_id == validation_set_id:
                latest = vset
        return latest

    def list_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationSetDTO]:
        latest_by_id: dict[str, ValidationSetDTO] = {}
        for vset in self._iter_all(ctx):
            if vset.run_id != run_id:
                continue
            latest_by_id[vset.validation_set_id] = vset
        # Most-recent first by created_at — testers expect to see
        # the freshly generated set at the top of the FE list.
        return sorted(
            latest_by_id.values(),
            key=lambda v: v.created_at,
            reverse=True,
        )

    def purge_for_run(self, ctx: ProjectContext, run_id: str) -> int:
        """Rewrite the JSONL file minus every snapshot whose
        `run_id` matches. Used by the hard-delete (purge) cascade
        so a purged run doesn't leave dangling validation sets.
        Returns the number of removed snapshots."""
        return _purge_jsonl_by_run_id(self._path(ctx), run_id)

    def _iter_all(self, ctx: ProjectContext) -> Iterable[ValidationSetDTO]:
        path = self._path(ctx)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield _set_from_payload(payload)


# ---- ValidationRunStore --------------------------------------------


class JsonlValidationRunStore:
    """Same pattern as ValidationSetStore. Upserted multiple times
    per run lifecycle: `pending` → `running` → terminal."""

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def _path(self, ctx: ProjectContext):
        return (
            self._workspace.area(ctx, WorkspaceArea.VALIDATION)
            / VALIDATION_RUNS_FILENAME
        )

    def upsert(self, ctx: ProjectContext, vrun: ValidationRunDTO) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(vrun), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    def get(
        self, ctx: ProjectContext, validation_run_id: str,
    ) -> ValidationRunDTO | None:
        latest: ValidationRunDTO | None = None
        for vrun in self._iter_all(ctx):
            if vrun.validation_run_id == validation_run_id:
                latest = vrun
        return latest

    def list_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationRunDTO]:
        latest_by_id: dict[str, ValidationRunDTO] = {}
        for vrun in self._iter_all(ctx):
            if vrun.run_id != run_id:
                continue
            latest_by_id[vrun.validation_run_id] = vrun
        return sorted(
            latest_by_id.values(),
            key=lambda v: v.started_at,
            reverse=True,
        )

    def purge_for_run(self, ctx: ProjectContext, run_id: str) -> int:
        """Same shape as `JsonlValidationSetStore.purge_for_run` —
        cascade-delete every validation-run snapshot for `run_id`."""
        return _purge_jsonl_by_run_id(self._path(ctx), run_id)

    def _iter_all(self, ctx: ProjectContext) -> Iterable[ValidationRunDTO]:
        path = self._path(ctx)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield _run_from_payload(payload)


# ---- Hydration helpers (payload → typed DTO) -----------------------
#
# Defensive about producer drift: missing optional fields fall back
# to dataclass defaults rather than raising. This keeps reads
# resilient against future producers that add fields after the
# reader is deployed.


def _set_from_payload(payload: dict) -> ValidationSetDTO:
    raw_cases = payload.get("test_cases") or []
    test_cases = [_test_case_from_payload(c) for c in raw_cases if isinstance(c, dict)]
    return ValidationSetDTO(
        validation_set_id=str(payload.get("validation_set_id", "")),
        run_id=str(payload.get("run_id", "")),
        document_ids=list(payload.get("document_ids") or []),
        source=payload.get("source", "generated"),
        status=payload.get("status", "draft"),
        created_at=str(payload.get("created_at", "")),
        created_by=payload.get("created_by"),
        generator_version=payload.get("generator_version"),
        artifacts_content_hash=payload.get("artifacts_content_hash"),
        test_cases=test_cases,
        metadata=dict(payload.get("metadata") or {}),
    )


def _test_case_from_payload(payload: dict) -> ValidationTestCaseDTO:
    return ValidationTestCaseDTO(
        test_case_id=str(payload.get("test_case_id", "")),
        question=str(payload.get("question", "")),
        type=payload.get("type", "retrieval"),
        priority=payload.get("priority", "normal"),
        expected_behavior=payload.get("expected_behavior", "answer_with_citations"),
        expected_answer_points=list(payload.get("expected_answer_points") or []),
        expected_chunks=list(payload.get("expected_chunks") or []),
        expected_pages=[int(p) for p in (payload.get("expected_pages") or []) if isinstance(p, (int, float))],
        expected_artifacts=list(payload.get("expected_artifacts") or []),
        expected_graph_nodes=list(payload.get("expected_graph_nodes") or []),
        expected_graph_edges=list(payload.get("expected_graph_edges") or []),
        citation_required=bool(payload.get("citation_required") or False),
        source_traceability=list(payload.get("source_traceability") or []),
        metadata=dict(payload.get("metadata") or {}),
    )


def _run_from_payload(payload: dict) -> ValidationRunDTO:
    raw_results = payload.get("results") or []
    results = [_result_from_payload(r) for r in raw_results if isinstance(r, dict)]
    return ValidationRunDTO(
        validation_run_id=str(payload.get("validation_run_id", "")),
        validation_set_id=str(payload.get("validation_set_id", "")),
        run_id=str(payload.get("run_id", "")),
        execution_status=payload.get("execution_status", "pending"),
        validation_status=payload.get("validation_status", "inconclusive"),
        started_at=str(payload.get("started_at", "")),
        completed_at=payload.get("completed_at"),
        actor=str(payload.get("actor", "system")),
        summary=_summary_from_payload(payload.get("summary") or {}),
        results=results,
        failure_message=payload.get("failure_message"),
        metadata=dict(payload.get("metadata") or {}),
    )


def _result_from_payload(payload: dict) -> ValidationResultDTO:
    raw_chunks = payload.get("retrieved_chunks") or []
    chunks = [_chunk_ref_from_payload(c) for c in raw_chunks if isinstance(c, dict)]
    raw_citations = payload.get("citations") or []
    citations = [_citation_from_payload(c) for c in raw_citations if isinstance(c, dict)]
    raw_checks = payload.get("checks") or []
    checks = [_check_from_payload(c) for c in raw_checks if isinstance(c, dict)]
    return ValidationResultDTO(
        result_id=str(payload.get("result_id", "")),
        test_case_id=str(payload.get("test_case_id", "")),
        status=payload.get("status", "skipped"),
        question=str(payload.get("question", "")),
        answer=str(payload.get("answer", "")),
        retrieved_chunks=chunks,
        citations=citations,
        checks=checks,
        judge_notes=payload.get("judge_notes"),
        failure_reason=payload.get("failure_reason"),
        tester_verdict=payload.get("tester_verdict"),
        tester_notes=payload.get("tester_notes"),
    )


def _chunk_ref_from_payload(payload: dict) -> RetrievedChunkRefDTO:
    return RetrievedChunkRefDTO(
        artifact_id=str(payload.get("artifact_id", "")),
        chunk_id=payload.get("chunk_id"),
        run_id=payload.get("run_id"),
        document_id=payload.get("document_id"),
        source_location=payload.get("source_location"),
        score=float(payload.get("score") or 0.0),
        preview=str(payload.get("preview", "")),
        artifact_kind=payload.get("artifact_kind"),
    )


def _citation_from_payload(payload: dict) -> ValidationCitationDTO:
    return ValidationCitationDTO(
        artifact_id=str(payload.get("artifact_id", "")),
        artifact_type=str(payload.get("artifact_type", "")),
        source_document_id=payload.get("source_document_id"),
        source_location=payload.get("source_location"),
        chunk_id=payload.get("chunk_id"),
        run_id=payload.get("run_id"),
    )


def _check_from_payload(payload: dict) -> ValidationCheckDTO:
    return ValidationCheckDTO(
        name=str(payload.get("name", "")),
        severity=payload.get("severity", "required"),
        passed=bool(payload.get("passed") or False),
        detail=payload.get("detail"),
        expected=payload.get("expected"),
        actual=payload.get("actual"),
    )


def _summary_from_payload(payload: dict) -> ValidationSummaryDTO:
    coverage_payload = payload.get("coverage") or {}
    coverage = ValidationCoverageDTO(
        by_type=dict(coverage_payload.get("by_type") or {}),
        by_priority=dict(coverage_payload.get("by_priority") or {}),
        by_section=dict(coverage_payload.get("by_section") or {}),
    )
    return ValidationSummaryDTO(
        total=int(payload.get("total") or 0),
        passed=int(payload.get("passed") or 0),
        warning=int(payload.get("warning") or 0),
        failed=int(payload.get("failed") or 0),
        skipped=int(payload.get("skipped") or 0),
        coverage=coverage,
        main_issues=list(payload.get("main_issues") or []),
        recommended_action=payload.get("recommended_action"),
    )


def _purge_jsonl_by_run_id(path, run_id: str) -> int:
    """Atomically rewrite an append-only JSONL file with all snapshots
    for `run_id` removed. Returns the number of removed lines.

    Shared by both validation stores because the read shape is
    identical (one JSON object per line, top-level `run_id` field).
    Atomic via tmp-file + rename so a mid-purge crash can't corrupt
    the file. No-op when the file doesn't exist."""
    if not path.exists():
        return 0
    kept: list[str] = []
    removed = 0
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                kept.append(stripped)  # preserve unparseable lines
                continue
            if str(payload.get("run_id")) == run_id:
                removed += 1
                continue
            kept.append(stripped)
    if removed == 0:
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for line in kept:
            fh.write(line)
            fh.write("\n")
    tmp.replace(path)
    return removed


# Re-exported for the service-layer import surface — the service
# constructs DTOs and hands them to the store, never the other way
# around, so this re-export only exists so callers don't need two
# imports.
_ = Any  # appeases pyflakes when running unused-import checks.
