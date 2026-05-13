"""Per-run ingestion diagnostic recorder.

Phase-1 instrumentation surface added in response to the "why is
ingestion slow?" question. Captures stage-level timing, LLM call
summaries, and enrichment progress into one structured record per
ingestion run, then persists the aggregate as an
``ingestion_diagnostic_report`` artifact at terminal time.

Design rules (all load-bearing):

  * **Optional collaborator.** Every consumer accepts a
    ``DiagnosticRecorder | None`` and is a no-op when ``None``.
    Pre-refactor wiring keeps working unchanged.

  * **Never breaks the workflow.** Every public method is wrapped
    so its own failures log at WARNING and propagate ``None`` /
    the original return value to the caller. Instrumentation that
    crashes the ingest is worse than no instrumentation.

  * **No document content.** Counters, IDs, durations, token
    estimates only. Free-text fields cap at 80 characters
    (``_safe_preview``) so a stray exception message can't leak
    a chunk of source text.

  * **Additive event names.** New audit events use the
    ``j1.ingestion.*`` prefix and never rename or shadow existing
    ``processing.*`` / ``j1.progress.*`` events.

  * **In-memory aggregation.** The recorder accumulates per-run
    state in a thread-safe dict, then serialises one JSON artifact
    at ``write_report`` time. No partial writes mid-run — the
    artifact is the canonical view; the audit stream is the
    real-time view (operator can tail it during long runs).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from j1.artifacts.registry import ArtifactRegistry
    from j1.audit.recorder import AuditRecorder
    from j1.projects.context import ProjectContext
    from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.processing.diagnostics")


# ---- Event names -------------------------------------------------
#
# Stable audit ``action=`` strings emitted by the recorder. Kept as
# module-level constants so log consumers (ops dashboards, this
# project's own tests) can match against them without depending on
# strings buried in call sites.

EVENT_STAGE_STARTED = "j1.ingestion.stage.started"
EVENT_STAGE_COMPLETED = "j1.ingestion.stage.completed"
EVENT_LLM_CALL_COMPLETED = "j1.ingestion.llm_call.completed"
EVENT_ENRICHMENT_PROGRESS = "j1.ingestion.enrichment.progress"
EVENT_REPORT_WRITTEN = "j1.ingestion.diagnostic_report.written"

# Compile-attempt + retry trace. Emitted from the workflow's
# compile-retry ladder so audit consumers can see every attempt
# (FAST → STANDARD → DEEP → STOP) and the operator-visible reason
# each retry was scheduled. Without these, the events stream
# carried a single ``processing.compile.completed`` per document
# with no visibility into how many attempts ran or why an
# escalation fired.
EVENT_COMPILE_ATTEMPT_STARTED = "j1.ingestion.compile.attempt.started"
EVENT_COMPILE_ATTEMPT_COMPLETED = "j1.ingestion.compile.attempt.completed"
EVENT_COMPILE_RETRY_SCHEDULED = "j1.ingestion.compile.retry.scheduled"

# Enrichment-attempt trace. Per-artifact enrich invocation in the
# composite path. Companion events to ``EVENT_ENRICHMENT_PROGRESS``
# (which carries the aggregate counters) so consumers can attribute
# each LLM call to a specific enrich attempt against a specific
# artifact.
EVENT_ENRICHMENT_ATTEMPT_STARTED = "j1.ingestion.enrichment.attempt.started"
EVENT_ENRICHMENT_ATTEMPT_COMPLETED = "j1.ingestion.enrichment.attempt.completed"
EVENT_ENRICHMENT_RETRY_SCHEDULED = "j1.ingestion.enrichment.retry.scheduled"


# ---- Artifact kind -----------------------------------------------

ARTIFACT_KIND_DIAGNOSTIC_REPORT = "compiled.ingestion_diagnostic_report"
DIAGNOSTIC_REPORT_FILENAME = "ingestion_diagnostic_report.json"


# ---- DTOs --------------------------------------------------------


@dataclass
class _StageRecord:
    name: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    success: bool = True
    error: str | None = None
    counters: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat()
                if self.completed_at else None
            ),
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
            "counters": dict(self.counters),
        }


@dataclass
class _LLMCallRecord:
    stage: str
    purpose: str
    provider: str | None
    model: str | None
    duration_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    attempts: int = 1
    retried: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "purpose": self.purpose,
            "provider": self.provider,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "attempts": self.attempts,
            "retried": self.retried,
            "error": self.error,
        }


@dataclass
class _EnrichmentProgressRecord:
    planned: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    status: str = "running"  # COMPLETED / PARTIAL_ENRICHMENT / SKIPPED_BY_POLICY / FAILED / TIMED_OUT
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class _RunDiagnostics:
    run_id: str
    document_id: str | None = None
    filename: str | None = None
    stages: list[_StageRecord] = field(default_factory=list)
    llm_calls: list[_LLMCallRecord] = field(default_factory=list)
    enrichment: _EnrichmentProgressRecord = field(
        default_factory=_EnrichmentProgressRecord,
    )
    counters: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---- Recorder ----------------------------------------------------


class DiagnosticRecorder:
    """Per-worker singleton — collects diagnostics across activities.

    Constructed once at bootstrap (see ``deploy/dev/_wiring.py``)
    and passed to every activity surface that wants instrumentation
    (``ProcessingActivities``, ``RunsActivities``, the MinerU
    bridge). Lifetime spans the worker process; per-run state is
    kept in ``_by_run`` and dropped after the report is written or
    if the run never reaches terminal.
    """

    def __init__(
        self,
        *,
        audit: "AuditRecorder | None" = None,
        artifact_registry: "ArtifactRegistry | None" = None,
        workspace: "WorkspaceResolver | None" = None,
        clock=None,
    ) -> None:
        self._audit = audit
        self._artifacts = artifact_registry
        self._workspace = workspace
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._by_run: dict[str, _RunDiagnostics] = {}
        self._lock = threading.Lock()

    # ---- Stage timing --------------------------------------------

    @contextmanager
    def stage(
        self,
        *,
        ctx: "ProjectContext",
        run_id: str | None,
        stage_name: str,
        counters: dict[str, int] | None = None,
        document_id: str | None = None,
    ) -> Iterator["_StageHandle"]:
        """Context manager: ``with recorder.stage(run_id=..., stage_name=...) as st: st.update(chunk_count=N)``.

        Records started/completed/duration and emits the two stage
        audit events. Failures inside the ``with`` block flip the
        record to ``success=False`` + capture the exception type
        name (NEVER the full traceback or message body) and
        re-raise.
        """
        if run_id is None:
            # Unwired path — yield a no-op handle so the with block
            # works without changes to the caller. Nothing recorded.
            yield _StageHandle(_no_op=True)
            return

        record = _StageRecord(
            name=stage_name,
            started_at=self._safe_now(),
            counters=dict(counters or {}),
        )
        with self._lock:
            run = self._ensure_run(run_id, document_id)
            run.stages.append(record)
        self._safe_audit(
            ctx=ctx, action=EVENT_STAGE_STARTED,
            target_id=run_id,
            payload={
                "run_id": run_id,
                "document_id": document_id,
                "stage": stage_name,
                "counters": dict(record.counters),
            },
        )
        handle = _StageHandle(record=record)
        started_perf = time.perf_counter()
        try:
            yield handle
        except Exception as exc:  # noqa: BLE001
            record.success = False
            record.error = type(exc).__name__
            self._finalize_stage(
                ctx=ctx, run_id=run_id, document_id=document_id,
                record=record, started_perf=started_perf,
            )
            raise
        else:
            self._finalize_stage(
                ctx=ctx, run_id=run_id, document_id=document_id,
                record=record, started_perf=started_perf,
            )

    def record_stage_event(
        self,
        *,
        ctx: "ProjectContext",
        run_id: str | None,
        stage_name: str,
        duration_ms: int,
        counters: dict[str, int] | None = None,
        document_id: str | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        """Drop-in for code paths that already measured their own
        timing (e.g. the MinerU bridge tracks ``parse_elapsed_ms``).

        Records the stage AS-IF the recorder had wrapped it,
        emitting both the started + completed audit events
        synchronously."""
        if run_id is None:
            return
        try:
            now = self._safe_now()
            record = _StageRecord(
                name=stage_name,
                started_at=now,
                completed_at=now,
                duration_ms=int(duration_ms),
                success=success,
                error=_safe_preview(error),
                counters=dict(counters or {}),
            )
            with self._lock:
                run = self._ensure_run(run_id, document_id)
                run.stages.append(record)
            self._safe_audit(
                ctx=ctx, action=EVENT_STAGE_STARTED,
                target_id=run_id,
                payload={
                    "run_id": run_id,
                    "document_id": document_id,
                    "stage": stage_name,
                    "counters": dict(record.counters),
                },
            )
            self._safe_audit(
                ctx=ctx, action=EVENT_STAGE_COMPLETED,
                target_id=run_id,
                payload={
                    "run_id": run_id,
                    "document_id": document_id,
                    "stage": stage_name,
                    "duration_ms": record.duration_ms,
                    "success": success,
                    "error": record.error,
                    "counters": dict(record.counters),
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: record_stage_event failed",
                exc_info=True,
            )

    # ---- LLM calls -----------------------------------------------

    def record_llm_call(
        self,
        *,
        ctx: "ProjectContext",
        run_id: str | None,
        stage: str,
        purpose: str,
        provider: str | None,
        model: str | None,
        duration_ms: int,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        attempts: int = 1,
        retried: bool = False,
        error: str | None = None,
        document_id: str | None = None,
    ) -> None:
        if run_id is None:
            return
        try:
            record = _LLMCallRecord(
                stage=stage,
                purpose=purpose,
                provider=provider,
                model=model,
                duration_ms=int(duration_ms),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                attempts=int(attempts),
                retried=bool(retried),
                error=_safe_preview(error),
            )
            with self._lock:
                run = self._ensure_run(run_id, document_id)
                run.llm_calls.append(record)
            self._safe_audit(
                ctx=ctx, action=EVENT_LLM_CALL_COMPLETED,
                target_id=run_id,
                payload={
                    "run_id": run_id,
                    "stage": stage,
                    "purpose": purpose,
                    "provider": provider,
                    "model": model,
                    "duration_ms": record.duration_ms,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "attempts": attempts,
                    "retried": retried,
                    "error": record.error,
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: record_llm_call failed",
                exc_info=True,
            )

    # ---- Compile / enrichment attempt + retry trace --------------

    def record_attempt_event(
        self,
        *,
        ctx: "ProjectContext",
        run_id: str | None,
        action: str,
        attempt: int,
        document_id: str | None = None,
        artifact_id: str | None = None,
        mode: str | None = None,
        next_mode: str | None = None,
        duration_ms: int | None = None,
        success: bool | None = None,
        reason: str | None = None,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit a compile / enrichment attempt or retry-scheduled
        audit event. Generic so the workflow can use one helper for
        all six new event actions (compile.attempt.started /
        completed / retry.scheduled and enrichment counterparts).

        ``mode`` is the compile-retry ladder rung (FAST / STANDARD
        / DEEP / STOP) or the enricher kind. ``next_mode`` is what
        the retry will run as. ``reason`` is the operator-visible
        explanation (e.g. ``"insufficient_chunks"``) for a retry.
        """
        if run_id is None:
            return
        try:
            payload: dict[str, Any] = {
                "run_id": run_id,
                "document_id": document_id,
                "artifact_id": artifact_id,
                "attempt": int(attempt),
                "mode": mode,
                "next_mode": next_mode,
                "duration_ms": duration_ms,
                "success": success,
                "reason": _safe_preview(reason, limit=240),
                "error": _safe_preview(error),
            }
            if extra:
                for k, v in extra.items():
                    payload.setdefault(k, v)
            self._safe_audit(
                ctx=ctx, action=action,
                target_id=run_id,
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: record_attempt_event failed "
                "(action=%s)", action, exc_info=True,
            )

    # ---- Enrichment progress -------------------------------------

    def record_enrichment_progress(
        self,
        *,
        ctx: "ProjectContext",
        run_id: str | None,
        planned: int | None = None,
        completed: int | None = None,
        skipped: int | None = None,
        failed: int | None = None,
        status: str | None = None,
        detail: str | None = None,
        document_id: str | None = None,
    ) -> None:
        if run_id is None:
            return
        try:
            with self._lock:
                run = self._ensure_run(run_id, document_id)
                if planned is not None:
                    run.enrichment.planned = int(planned)
                if completed is not None:
                    run.enrichment.completed = int(completed)
                if skipped is not None:
                    run.enrichment.skipped = int(skipped)
                if failed is not None:
                    run.enrichment.failed = int(failed)
                if status is not None:
                    run.enrichment.status = status
                if detail is not None:
                    run.enrichment.detail = _safe_preview(detail)
                snapshot = run.enrichment.to_dict()
            self._safe_audit(
                ctx=ctx, action=EVENT_ENRICHMENT_PROGRESS,
                target_id=run_id,
                payload={
                    "run_id": run_id,
                    "document_id": document_id,
                    **snapshot,
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: record_enrichment_progress failed",
                exc_info=True,
            )

    # ---- Counter accumulators ------------------------------------

    def add_counter(
        self,
        *,
        run_id: str | None,
        name: str,
        delta: int = 1,
        document_id: str | None = None,
    ) -> None:
        if run_id is None:
            return
        try:
            with self._lock:
                run = self._ensure_run(run_id, document_id)
                run.counters[name] = run.counters.get(name, 0) + int(delta)
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: add_counter failed", exc_info=True,
            )

    def set_metadata(
        self,
        *,
        run_id: str | None,
        document_id: str | None = None,
        filename: str | None = None,
    ) -> None:
        if run_id is None:
            return
        try:
            with self._lock:
                run = self._ensure_run(run_id, document_id)
                if document_id is not None:
                    run.document_id = document_id
                if filename is not None:
                    run.filename = filename
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: set_metadata failed", exc_info=True,
            )

    def add_warning(
        self,
        *,
        run_id: str | None,
        message: str,
    ) -> None:
        if run_id is None:
            return
        try:
            with self._lock:
                run = self._ensure_run(run_id)
                run.warnings.append(_safe_preview(message, limit=240))
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: add_warning failed", exc_info=True,
            )

    # ---- Report writer -------------------------------------------

    def build_report(self, run_id: str) -> dict[str, Any] | None:
        """Materialise the in-memory record into the JSON shape the
        ``ingestion_diagnostic_report`` artifact carries.

        Returns ``None`` when the run was never recorded against
        (no stages, no LLM calls, no enrichment). Callers that get
        ``None`` should skip the artifact write — there's nothing
        to report."""
        with self._lock:
            run = self._by_run.get(run_id)
            if run is None:
                return None
            if (
                not run.stages and not run.llm_calls
                and run.enrichment.planned == 0
                and run.enrichment.completed == 0
            ):
                return None
            stages = [s.to_dict() for s in run.stages]
            llm_calls = list(run.llm_calls)
            counters = dict(run.counters)
            enrichment = run.enrichment.to_dict()
            warnings = list(run.warnings)
            document_id = run.document_id
            filename = run.filename

        return _materialise_report(
            run_id=run_id,
            document_id=document_id,
            filename=filename,
            stages=stages,
            llm_calls=llm_calls,
            enrichment=enrichment,
            counters=counters,
            warnings=warnings,
        )

    def write_report(
        self,
        *,
        ctx: "ProjectContext",
        run_id: str,
        document_id: str | None = None,
        filename: str | None = None,
    ) -> str | None:
        """Write the per-run diagnostic JSON as an artifact + emit
        ``j1.ingestion.diagnostic_report.written``.

        Returns the artifact id, or ``None`` when the report had
        nothing to write or any IO step failed (logged at WARNING).
        Always clears the in-memory record for ``run_id`` so a
        rerun starts fresh."""
        if document_id or filename:
            self.set_metadata(
                run_id=run_id, document_id=document_id, filename=filename,
            )
        try:
            report = self.build_report(run_id)
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: build_report failed", exc_info=True,
            )
            report = None
        # Always drop in-memory state — even when nothing was written
        # we don't want stale runs accumulating in the worker.
        with self._lock:
            self._by_run.pop(run_id, None)
        if report is None:
            return None
        if self._artifacts is None or self._workspace is None:
            _log.info(
                "diagnostic recorder: report ready but no artifact "
                "registry / workspace wired; emitting audit event only "
                "(run_id=%s)", run_id,
            )
            self._safe_audit(
                ctx=ctx, action=EVENT_REPORT_WRITTEN,
                target_id=run_id,
                payload={
                    "run_id": run_id,
                    "document_id": document_id,
                    "filename": filename,
                    "artifact_id": None,
                    "stage_count": len(report.get("stages", [])),
                    "llm_call_count": report.get("llm_summary", {}).get(
                        "total_calls", 0,
                    ),
                },
            )
            return None
        try:
            artifact_id = self._write_artifact(ctx, run_id, report)
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: write_artifact failed (run_id=%s)",
                run_id, exc_info=True,
            )
            artifact_id = None
        self._safe_audit(
            ctx=ctx, action=EVENT_REPORT_WRITTEN,
            target_id=run_id,
            payload={
                "run_id": run_id,
                "document_id": document_id,
                "filename": filename,
                "artifact_id": artifact_id,
                "stage_count": len(report.get("stages", [])),
                "llm_call_count": report.get("llm_summary", {}).get(
                    "total_calls", 0,
                ),
            },
        )
        return artifact_id

    # ---- Internals -----------------------------------------------

    def _ensure_run(
        self, run_id: str, document_id: str | None = None,
    ) -> _RunDiagnostics:
        run = self._by_run.get(run_id)
        if run is None:
            run = _RunDiagnostics(run_id=run_id, document_id=document_id)
            self._by_run[run_id] = run
        elif document_id is not None and run.document_id is None:
            run.document_id = document_id
        return run

    def _finalize_stage(
        self,
        *,
        ctx: "ProjectContext",
        run_id: str,
        document_id: str | None,
        record: _StageRecord,
        started_perf: float,
    ) -> None:
        record.completed_at = self._safe_now()
        record.duration_ms = int(
            (time.perf_counter() - started_perf) * 1000,
        )
        self._safe_audit(
            ctx=ctx, action=EVENT_STAGE_COMPLETED,
            target_id=run_id,
            payload={
                "run_id": run_id,
                "document_id": document_id,
                "stage": record.name,
                "duration_ms": record.duration_ms,
                "success": record.success,
                "error": record.error,
                "counters": dict(record.counters),
            },
        )

    def _safe_audit(
        self,
        *,
        ctx: "ProjectContext",
        action: str,
        target_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> None:
        if self._audit is None:
            return
        try:
            # ``j1.ingestion.*`` events carry the same run_id in
            # target_id (the diagnostic surface is always run-scoped),
            # so default ``correlation_id`` to ``target_id`` when the
            # caller didn't override. Without this the recorder used
            # to emit events with ``correlation_id=None`` even though
            # the run was unambiguous — operators tailing
            # ``/ingestion-runs/{id}/events`` couldn't filter to the
            # current run.
            self._audit.record(
                ctx,
                actor="system",
                action=action,
                target_kind="ingestion_run",
                target_id=target_id,
                correlation_id=correlation_id or target_id,
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "diagnostic recorder: audit emit failed (action=%s)",
                action, exc_info=True,
            )

    def _safe_now(self) -> datetime:
        try:
            return self._clock()
        except Exception:  # noqa: BLE001
            return datetime.now(timezone.utc)

    def _write_artifact(
        self,
        ctx: "ProjectContext",
        run_id: str,
        report: dict[str, Any],
    ) -> str:
        """Persist the JSON report as a ``compiled.*`` artifact.

        Layout mirrors the existing ``compile_strategy_report``
        artifact — same area, same registry, same naming
        convention. Operators inspecting the workspace find it
        next to the strategy report without learning a new path."""
        # Lazy imports to keep the module's import cost low when the
        # recorder is wired-but-never-invoked.
        from j1.artifacts.models import ArtifactRecord
        from j1.jobs.status import ProcessingStatus, ReviewStatus
        from j1.workspace.layout import WorkspaceArea

        area_dir = self._workspace.area(ctx, WorkspaceArea.COMPILED)
        area_dir.mkdir(parents=True, exist_ok=True)
        artifact_id = f"diag-{run_id}-{uuid.uuid4().hex[:8]}"
        stored_name = f"{artifact_id}.json"
        path = area_dir / stored_name
        body = json.dumps(report, indent=2, sort_keys=False).encode("utf-8")
        path.write_bytes(body)
        record = ArtifactRecord(
            artifact_id=artifact_id,
            project=ctx,
            kind=ARTIFACT_KIND_DIAGNOSTIC_REPORT,
            location=f"{WorkspaceArea.COMPILED.value}/{stored_name}",
            content_hash=f"sha256:{artifact_id}",
            byte_size=len(body),
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=self._safe_now(),
            updated_at=self._safe_now(),
            source_document_ids=(
                [report["document_id"]]
                if report.get("document_id") else []
            ),
            metadata={
                "run_id": run_id,
                "title": DIAGNOSTIC_REPORT_FILENAME,
                "report_kind": "ingestion_diagnostic_report",
            },
        )
        # ``_raw_add`` skips the lineage guard; the report carries
        # ``run_id`` in metadata so it's compliant anyway, but the
        # guard is for ``chunk`` / ``graph_json`` shapes specifically.
        self._artifacts._raw_add(record)  # type: ignore[union-attr]
        return artifact_id


# ---- Stage handle ------------------------------------------------


@dataclass
class _StageHandle:
    """Returned by ``recorder.stage(...)`` as the ``with ... as`` target.

    Provides ``update`` for incrementally adding counters as the
    stage runs (e.g. the compile stage knows ``chunk_count`` only
    after MinerU returns)."""

    record: _StageRecord | None = None
    _no_op: bool = False

    def update(self, **counters: int) -> None:
        if self._no_op or self.record is None:
            return
        for k, v in counters.items():
            try:
                self.record.counters[k] = int(v)
            except (TypeError, ValueError):
                continue


# ---- Helpers -----------------------------------------------------


def _safe_preview(value: str | None, *, limit: int = 80) -> str | None:
    """Trim a free-text field to ``limit`` chars so a stray error
    message can't leak document content into the audit log."""
    if value is None:
        return None
    s = str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _materialise_report(
    *,
    run_id: str,
    document_id: str | None,
    filename: str | None,
    stages: list[dict[str, Any]],
    llm_calls: list[_LLMCallRecord],
    enrichment: dict[str, Any],
    counters: dict[str, int],
    warnings: list[str],
) -> dict[str, Any]:
    total_calls = len(llm_calls)
    total_input = sum(c.input_tokens or 0 for c in llm_calls)
    total_output = sum(c.output_tokens or 0 for c in llm_calls)
    total_errors = sum(1 for c in llm_calls if c.error is not None)
    total_retries = sum(1 for c in llm_calls if c.retried)
    total_duration_ms = sum(c.duration_ms for c in llm_calls)
    by_purpose: dict[str, int] = {}
    for c in llm_calls:
        by_purpose[c.purpose] = by_purpose.get(c.purpose, 0) + 1

    # Bottleneck candidates: top 5 stages by duration. Cheap to
    # compute here so consumers don't have to re-derive.
    sorted_stages = sorted(
        (s for s in stages if s.get("duration_ms") is not None),
        key=lambda s: s["duration_ms"] or 0,
        reverse=True,
    )
    total_stage_ms = sum(s.get("duration_ms") or 0 for s in stages)
    bottlenecks = []
    for s in sorted_stages[:5]:
        d = s.get("duration_ms") or 0
        bottlenecks.append({
            "stage": s["name"],
            "duration_ms": d,
            "share": round(d / total_stage_ms, 3)
            if total_stage_ms else 0.0,
        })

    return {
        "schema_version": 1,
        "run_id": run_id,
        "document_id": document_id,
        "filename": filename,
        "stages": stages,
        "counters": counters,
        "llm_summary": {
            "total_calls": total_calls,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_duration_ms": total_duration_ms,
            "errors": total_errors,
            "retries": total_retries,
            "by_purpose": by_purpose,
        },
        "llm_calls": [c.to_dict() for c in llm_calls],
        "enrichment_summary": enrichment,
        "warnings": warnings,
        "bottleneck_candidates": bottlenecks,
    }


__all__ = [
    "ARTIFACT_KIND_DIAGNOSTIC_REPORT",
    "DIAGNOSTIC_REPORT_FILENAME",
    "EVENT_COMPILE_ATTEMPT_COMPLETED",
    "EVENT_COMPILE_ATTEMPT_STARTED",
    "EVENT_COMPILE_RETRY_SCHEDULED",
    "EVENT_ENRICHMENT_ATTEMPT_COMPLETED",
    "EVENT_ENRICHMENT_ATTEMPT_STARTED",
    "EVENT_ENRICHMENT_PROGRESS",
    "EVENT_ENRICHMENT_RETRY_SCHEDULED",
    "EVENT_LLM_CALL_COMPLETED",
    "EVENT_REPORT_WRITTEN",
    "EVENT_STAGE_COMPLETED",
    "EVENT_STAGE_STARTED",
    "DiagnosticRecorder",
    "current_run_context",
    "RunContext",
    "set_current_run_context",
]


# ---- Run-scoped context propagation ------------------------------
#
# The activity (compile/enrich/build_graph/index) holds the recorder
# + run_id, but the LLM call sites live many frames below in the
# enrichment / RAGAnything bridge stack. Passing the recorder through
# every call would touch dozens of files. Instead, the activity
# sets a ``contextvars`` value at entry and the limiter / LLM client
# reads it from there — same per-task isolation Temporal already
# uses internally, no thread-local leakage between concurrent
# activity invocations.

import contextvars as _contextvars


@dataclass(frozen=True)
class RunContext:
    """What the limiter needs to attribute an LLM call to a run."""

    run_id: str
    document_id: str | None
    stage: str
    recorder: "DiagnosticRecorder"
    ctx: Any  # ProjectContext — kept Any to avoid the heavy import


_current_run_context: _contextvars.ContextVar[RunContext | None] = (
    _contextvars.ContextVar(
        "j1_diag_current_run_context", default=None,
    )
)


def current_run_context() -> RunContext | None:
    """Return the active diagnostic run context for this task, or
    ``None`` when no activity has set one."""
    return _current_run_context.get()


@contextmanager
def set_current_run_context(rc: RunContext | None) -> Iterator[None]:
    """Context manager: bind ``rc`` as the current run context for
    the duration of the ``with`` block. Restores the previous value
    on exit (works correctly for nested calls)."""
    token = _current_run_context.set(rc)
    try:
        yield
    finally:
        _current_run_context.reset(token)
