"""Ingestion-run persistence + progress reporting.

The `runs` package layers a user-facing progress surface over the
existing Temporal-backed workflow + audit infrastructure. It does NOT
replace either: workflows remain the orchestration engine, the audit
log remains the persistent event store. `IngestionRun` records and
`ProgressEvent`s are projections / additional records that make the
ingestion lifecycle navigable by a frontend without reading workflow
history or audit JSONL directly.

Public surface:

 * `IngestionRun` / `RunStatus` — the persistent run-record
 * `IngestionRunStore` / `JsonlIngestionRunStore` — store interface
 + JSONL-backed implementation (mirrors `JsonlAuditSink`)
 * `ProgressReporter` — Protocol the workflow / activities call
 * `AuditProgressReporter` — writes through the existing
 `AuditRecorder` so events are visible in the audit log + via the
 existing `GET /ingestion-jobs/{id}/events` endpoint
 * `TemporalHeartbeatReporter` — pumps compact progress into Temporal
 activity heartbeats so the operator UI can see liveness
 * `CompositeProgressReporter` — fan-out to multiple targets
 * `NoopProgressReporter` — for unit tests
"""

from j1.runs.models import (
    EXECUTION_DECISION_CONDITIONAL,
    EXECUTION_DECISION_RUN,
    EXECUTION_DECISION_SKIP,
    FAILURE_CODE_ASSESSMENT_FAILED,
    FAILURE_CODE_CHUNK_FAILED,
    FAILURE_CODE_COMPILE_FAILED,
    FAILURE_CODE_EMPTY_DOCUMENT,
    FAILURE_CODE_INDEX_FAILED,
    FAILURE_CODE_VERIFICATION_FAILED,
    IngestionRun,
    LEGACY_TO_CANONICAL_STATUS,
    PROGRESS_SEVERITY_ERROR,
    PROGRESS_SEVERITY_INFO,
    PROGRESS_SEVERITY_WARNING,
    ProgressEvent,
    RunStatus,
    canonical_status,
    status_aliases,
)
from j1.runs.reporter import (
    ACTION_PROGRESS_ASSESSMENT_COMPLETED,
    ACTION_PROGRESS_ASSESSMENT_STARTED,
    ACTION_PROGRESS_DOCUMENT_RECEIVED,
    ACTION_PROGRESS_HUMAN_REVIEW_REQUIRED,
    ACTION_PROGRESS_PLAN_CONFIRMED,
    ACTION_PROGRESS_PLAN_GENERATED,
    ACTION_PROGRESS_PLAN_REVISED,
    ACTION_PROGRESS_RUN_CANCELLED,
    ACTION_PROGRESS_RUN_COMPLETED,
    ACTION_PROGRESS_RUN_CREATED,
    ACTION_PROGRESS_RUN_FAILED,
    ACTION_PROGRESS_STEP_COMPLETED,
    ACTION_PROGRESS_STEP_FAILED,
    ACTION_PROGRESS_STEP_PROGRESS,
    ACTION_PROGRESS_STEP_SKIPPED,
    ACTION_PROGRESS_STEP_STARTED,
    ACTION_PROGRESS_STEP_WARNING,
    AuditProgressReporter,
    CompositeProgressReporter,
    NoopProgressReporter,
    PROGRESS_ACTION_PREFIX,
    PROGRESS_EVENT_ASSESSMENT_COMPLETED,
    PROGRESS_EVENT_ASSESSMENT_STARTED,
    PROGRESS_EVENT_DOCUMENT_RECEIVED,
    PROGRESS_EVENT_HUMAN_REVIEW_REQUIRED,
    PROGRESS_EVENT_PLAN_CONFIRMED,
    PROGRESS_EVENT_PLAN_GENERATED,
    PROGRESS_EVENT_PLAN_REVISED,
    PROGRESS_EVENT_RUN_CANCELLED,
    PROGRESS_EVENT_RUN_COMPLETED,
    PROGRESS_EVENT_RUN_CREATED,
    PROGRESS_EVENT_RUN_FAILED,
    PROGRESS_EVENT_STEP_COMPLETED,
    PROGRESS_EVENT_STEP_FAILED,
    PROGRESS_EVENT_STEP_PROGRESS,
    PROGRESS_EVENT_STEP_SKIPPED,
    PROGRESS_EVENT_STEP_STARTED,
    PROGRESS_EVENT_STEP_WARNING,
    PROGRESS_TARGET_KIND,
    PROGRESS_TERMINAL_EVENT_TYPES,
    ProgressReporter,
    TemporalHeartbeatReporter,
    is_progress_action,
)
from j1.runs.store import IngestionRunStore, JsonlIngestionRunStore


def build_default_progress_reporter(audit_recorder) -> "ProgressReporter":
    """Standard composite reporter for production deployments.

 Fans out to:
 * `AuditProgressReporter` — writes through the deployment's
 `AuditRecorder`, persisting events into the workspace audit
 log (where the `/ingestion-runs/{id}/events` endpoint reads
 them).
 * `TemporalHeartbeatReporter` — pumps compact summaries into
 Temporal activity heartbeats so operator UIs (Temporal Web,
 worker logs) see liveness on long-running activities.

 Importing this from a deployment entrypoint avoids each
 deployment re-discovering the right composition. Tests can pass
 `NoopProgressReporter` directly instead.
 """
    return CompositeProgressReporter(
        AuditProgressReporter(audit_recorder),
        TemporalHeartbeatReporter(),
    )

__all__ = [
    "ACTION_PROGRESS_ASSESSMENT_COMPLETED",
    "ACTION_PROGRESS_ASSESSMENT_STARTED",
    "ACTION_PROGRESS_DOCUMENT_RECEIVED",
    "ACTION_PROGRESS_HUMAN_REVIEW_REQUIRED",
    "ACTION_PROGRESS_PLAN_CONFIRMED",
    "ACTION_PROGRESS_PLAN_GENERATED",
    "ACTION_PROGRESS_PLAN_REVISED",
    "ACTION_PROGRESS_RUN_CANCELLED",
    "ACTION_PROGRESS_RUN_COMPLETED",
    "ACTION_PROGRESS_RUN_CREATED",
    "ACTION_PROGRESS_RUN_FAILED",
    "ACTION_PROGRESS_STEP_COMPLETED",
    "ACTION_PROGRESS_STEP_FAILED",
    "ACTION_PROGRESS_STEP_PROGRESS",
    "ACTION_PROGRESS_STEP_SKIPPED",
    "ACTION_PROGRESS_STEP_STARTED",
    "ACTION_PROGRESS_STEP_WARNING",
    "AuditProgressReporter",
    "CompositeProgressReporter",
    "build_default_progress_reporter",
    "EXECUTION_DECISION_CONDITIONAL",
    "EXECUTION_DECISION_RUN",
    "EXECUTION_DECISION_SKIP",
    "FAILURE_CODE_ASSESSMENT_FAILED",
    "FAILURE_CODE_CHUNK_FAILED",
    "FAILURE_CODE_COMPILE_FAILED",
    "FAILURE_CODE_EMPTY_DOCUMENT",
    "FAILURE_CODE_INDEX_FAILED",
    "FAILURE_CODE_VERIFICATION_FAILED",
    "IngestionRun",
    "IngestionRunStore",
    "JsonlIngestionRunStore",
    "LEGACY_TO_CANONICAL_STATUS",
    "NoopProgressReporter",
    "PROGRESS_ACTION_PREFIX",
    "PROGRESS_EVENT_ASSESSMENT_COMPLETED",
    "PROGRESS_EVENT_ASSESSMENT_STARTED",
    "PROGRESS_EVENT_DOCUMENT_RECEIVED",
    "PROGRESS_EVENT_HUMAN_REVIEW_REQUIRED",
    "PROGRESS_EVENT_PLAN_CONFIRMED",
    "PROGRESS_EVENT_PLAN_GENERATED",
    "PROGRESS_EVENT_PLAN_REVISED",
    "PROGRESS_EVENT_RUN_CANCELLED",
    "PROGRESS_EVENT_RUN_COMPLETED",
    "PROGRESS_EVENT_RUN_CREATED",
    "PROGRESS_EVENT_RUN_FAILED",
    "PROGRESS_EVENT_STEP_COMPLETED",
    "PROGRESS_EVENT_STEP_FAILED",
    "PROGRESS_EVENT_STEP_PROGRESS",
    "PROGRESS_EVENT_STEP_SKIPPED",
    "PROGRESS_EVENT_STEP_STARTED",
    "PROGRESS_EVENT_STEP_WARNING",
    "PROGRESS_SEVERITY_ERROR",
    "PROGRESS_SEVERITY_INFO",
    "PROGRESS_SEVERITY_WARNING",
    "PROGRESS_TARGET_KIND",
    "PROGRESS_TERMINAL_EVENT_TYPES",
    "ProgressEvent",
    "ProgressReporter",
    "RunStatus",
    "TemporalHeartbeatReporter",
    "canonical_status",
    "is_progress_action",
    "status_aliases",
]
