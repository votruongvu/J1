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
    "IngestionRun",
    "PROGRESS_SEVERITY_ERROR",
    "PROGRESS_SEVERITY_INFO",
    "PROGRESS_SEVERITY_WARNING",
    "ProgressEvent",
    "RunStatus",
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
    once any in-flight activity finishes."""

    CREATED = "created"
    ASSESSING = "assessing"
    PLAN_READY = "plan_ready"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    RUNNING = "running"
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
