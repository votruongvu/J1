"""IngestionRun + ProgressEvent dataclasses + status enums.

These records are the user-facing projection of the underlying
Temporal workflow + audit log: an `IngestionRun` is one row per
document-ingestion attempt, and a `ProgressEvent` is one entry in
that run's timeline.

Field hygiene: nothing here stores document content, prompts, LLM
responses, or extracted text. All fields are operational metadata
safe to surface to a frontend / log aggregator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

__all__ = [
    "EXECUTION_DECISION_CONDITIONAL",
    "EXECUTION_DECISION_RUN",
    "EXECUTION_DECISION_SKIP",
    "FAILURE_CODE_ASSESSMENT_FAILED",
    "FAILURE_CODE_CHUNK_FAILED",
    "FAILURE_CODE_COMPILE_FAILED",
    "FAILURE_CODE_EMPTY_DOCUMENT",
    "FAILURE_CODE_ENRICHMENT_REQUIRED",
    "FAILURE_CODE_INDEX_FAILED",
    "FAILURE_CODE_VERIFICATION_FAILED",
    "IngestionRun",
    "LEGACY_TO_CANONICAL_STATUS",
    "PROGRESS_SEVERITY_ERROR",
    "PROGRESS_SEVERITY_INFO",
    "PROGRESS_SEVERITY_WARNING",
    "ProgressEvent",
    "RunStatus",
    "canonical_status",
    "status_aliases",
]


class RunStatus(StrEnum):
    """Lifecycle status of a single ingestion run.

    The state machine, from the user's perspective:

        CREATED  →  ASSESSING  →  PLAN_READY
                                     │
                                     ├──▶  WAITING_FOR_CONFIRMATION
                                     │             │
                                     │             ▼
                                     └──▶  RUNNING  ⇄  PAUSED
                                              │
                                              ▼
                                          CANCELLING (operator stop)
        ┌─────────────────────────────────────┤
        ▼                                     ▼
        SUCCEEDED                             FAILED
        SUCCEEDED_WITH_WARNINGS               CANCELLED
        REQUIRES_HUMAN_REVIEW                 (terminal)
        (terminal)

    `WAITING_FOR_CONFIRMATION` is reached only when the deployment
    has opted into manual confirmation (auto-run is the default; the
    confirmation gate is configured per-run via the ingest request).

    `PAUSED` and `CANCELLING` are operator-driven intermediate states.
    PAUSED is reversible (resume → RUNNING). CANCELLING is one-way:
    the workflow is winding down, will land at CANCELLED at terminal
    once any in-flight activity finishes.

    `COMPILE_PENDING` and `VERIFYING` are intermediate states for the
    two-phase compile model. The workflow parks at `COMPILE_PENDING`
    after assessment finishes; `POST /ingestion-runs/{id}/compile`
    advances it to RUNNING. `VERIFYING` runs immediately after the
    compile activity to gate chunk-count / index health checks; a
    failure here lands at terminal FAILED with one of the
    `FAILURE_CODE_*` reason codes.

    Canonical-vs-legacy names: `RECEIVED`, `ASSESSMENT_READY`, and
    `COMPILING` are the canonical names introduced by the
    macro-stage simplification (Phase 1 of the workflow refactor).
    `CREATED`, `PLAN_READY`, and `RUNNING` remain as readable
    legacy values for runs persisted by older worker builds — new
    runs SHOULD be written with the canonical names. See
    `canonical_status()` and `LEGACY_TO_CANONICAL_STATUS` for the
    translation table; downstream callers (REST status filters, FE
    predicate sets) treat each pair as equivalent."""

    CREATED = "created"
    RECEIVED = "received"
    ASSESSING = "assessing"
    PLAN_READY = "plan_ready"
    ASSESSMENT_READY = "assessment_ready"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    COMPILE_PENDING = "compile_pending"
    RUNNING = "running"
    COMPILING = "compiling"
    VERIFYING = "verifying"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REQUIRES_HUMAN_REVIEW = "requires_human_review"
    # Soft-delete tombstone. The run record + its artifacts stay on
    # disk for audit/compliance, but `_resolve_run_artifacts` excludes
    # them by default and listing endpoints exclude DELETED runs
    # unless the caller opts in via `?includeDeleted=true`.
    DELETED = "deleted"


# Execution-plan decisions (per-step). Mirrors the user-spec naming;
# kept as plain string constants so the audit-log payloads round-trip
# cleanly through JSONL without enum-vs-string ambiguity.
EXECUTION_DECISION_RUN = "RUN"
EXECUTION_DECISION_SKIP = "SKIP"
EXECUTION_DECISION_CONDITIONAL = "CONDITIONAL"


# Severity levels for `ProgressEvent`. Same string set used by the
# REST API, SSE stream, and audit payloads.
PROGRESS_SEVERITY_INFO = "INFO"
PROGRESS_SEVERITY_WARNING = "WARNING"
PROGRESS_SEVERITY_ERROR = "ERROR"


# Failure reason codes written to `IngestionRun.failure_code` when
# a structured macro-stage failure terminates a run. These are
# stable string values (operators filter on them in audit logs +
# the FE renders them as banner copy) — keep in sync with any
# corresponding labels in `frontend/src/pages/run-detail/`.
FAILURE_CODE_CHUNK_FAILED = "CHUNK_FAILED"
FAILURE_CODE_INDEX_FAILED = "INDEX_FAILED"
FAILURE_CODE_VERIFICATION_FAILED = "VERIFICATION_FAILED"
# Macro-stage failure codes (Phase 1). Distinct from the generic
# `ERROR_TYPE_REQUIRED_STEP_FAILED` label — they pin the failure
# to a specific macro stage so the FE/operator UI can render the
# right banner copy and link to the right diagnostic.
FAILURE_CODE_ASSESSMENT_FAILED = "ASSESSMENT_FAILED"
FAILURE_CODE_COMPILE_FAILED = "COMPILE_FAILED"
# Wave-6.5 enrichment-policy enforcement. Set on the run record
# when `require_enrichment_success=True` on the active domain
# policy AND the enrichment stage produced `status=failed`. The FE
# renders this as "enrichment was required and did not complete";
# raw compile artifacts remain readable on the run.
FAILURE_CODE_ENRICHMENT_REQUIRED = "ENRICHMENT_REQUIRED"
# Empty-document is NOT a failure in the operator sense; it's a
# successful early-out (the document had no extractable content).
# Surfaced as a failure_code on a terminal SUCCEEDED_WITH_WARNINGS
# run so the FE renders it as a non-error info banner — the
# vocabulary is shared with the failure-code table for consistency
# with how the existing rule-based skip path tags zero-content runs.
FAILURE_CODE_EMPTY_DOCUMENT = "EMPTY_DOCUMENT"


# Legacy → canonical translation table for the run-status enum.
# The two columns are equivalent for predicate-set membership and
# UI rendering; new code SHOULD use the canonical names but old
# runs persisted with legacy values keep working. Callers that
# need to compare statuses across the boundary use
# `canonical_status()` to fold legacy values onto the canonical
# row first. Empty for statuses with no legacy alias.
LEGACY_TO_CANONICAL_STATUS: dict[str, str] = {
    "created": "received",
    "plan_ready": "assessment_ready",
    "running": "running",
    # Note: `running` maps to itself — the legacy semantics
    # ("anything mid-pipeline") still apply, and `compiling` is
    # a more specific subset used only when the workflow is
    # actively inside the compile macro stage. Callers that need
    # the specific value read `current_stage` alongside.
}


def canonical_status(value: str | RunStatus) -> str:
    """Fold a legacy run-status string onto its canonical name.

    Used by predicate sets (REST status filters, FE active-state
    checks) so a query for `status=received` matches both runs
    written with the new name AND runs written with the legacy
    `created` value. Unknown values pass through unchanged."""
    raw = value.value if isinstance(value, RunStatus) else str(value)
    return LEGACY_TO_CANONICAL_STATUS.get(raw, raw)


def status_aliases(value: str | RunStatus) -> tuple[str, ...]:
    """Return all aliases that compare-equal to `value` (canonical
    + every legacy that maps to it). Used by the REST list filter
    to expand `?status=received` into `(received, created)` for
    the underlying store query."""
    canonical = canonical_status(value)
    aliases = [canonical]
    for legacy, mapped in LEGACY_TO_CANONICAL_STATUS.items():
        if mapped == canonical and legacy != canonical:
            aliases.append(legacy)
    return tuple(aliases)


@dataclass
class IngestionRun:
    """One ingestion attempt of one document.

    The `run_id` is the public identifier the frontend uses; it's
    distinct from `workflow_id` (Temporal's identifier) so the
    framework can hide Temporal-specific naming from end users.

    Mutability: this dataclass is mutable on purpose — the in-memory
    record is updated as the run progresses (status, current_stage,
    current_step, progress_percent, etc.) and the JSONL store
    appends a fresh snapshot on each update. Readers reconstruct the
    latest state by replaying the log."""

    run_id: str
    document_id: str
    workflow_id: str
    workflow_run_id: str | None
    status: RunStatus
    started_at: datetime
    updated_at: datetime
    workspace_id: str | None = None
    current_stage: str | None = None
    current_step: str | None = None
    progress_percent: int = 0
    completed_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    warning_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    def is_terminal(self) -> bool:
        return self.status in (
            RunStatus.SUCCEEDED,
            RunStatus.SUCCEEDED_WITH_WARNINGS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.REQUIRES_HUMAN_REVIEW,
        )


@dataclass(frozen=True)
class ProgressEvent:
    """One entry in an `IngestionRun`'s progress timeline.

    Persisted via the existing `AuditRecorder` (action=`j1.progress.*`)
    so historical events are queryable through the same JSONL audit
    log used for everything else; the SSE stream re-emits the same
    shape live. `event_id` is allocated by the audit recorder and
    matches the `AuditEvent.event_id` for the same entry — clients
    can use it as a resume cursor.

    Field hygiene: `message`, `engine`, `provider` are short
    operational strings. `metadata` is a small structured dict —
    NEVER document content, prompts, or model outputs."""

    event_id: str
    run_id: str
    event_type: str          # e.g. "step.progress", "plan.generated"
    timestamp: datetime
    severity: str = PROGRESS_SEVERITY_INFO
    stage: str | None = None
    step: str | None = None
    status: str | None = None
    progress_percent: int | None = None
    current: int | None = None
    total: int | None = None
    message: str | None = None
    engine: str | None = None
    provider: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
