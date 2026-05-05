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
    IngestionRun,
    PROGRESS_SEVERITY_ERROR,
    PROGRESS_SEVERITY_INFO,
    PROGRESS_SEVERITY_WARNING,
    ProgressEvent,
    RunStatus,
)
from j1.runs.reporter import (
    ACTION_PROGRESS_ASSESSMENT_COMPLETED,
    ACTION_PROGRESS_ASSESSMENT_STARTED,
    ACTION_PROGRESS_DOCUMENT_RECEIVED,
    ACTION_PROGRESS_HUMAN_REVIEW_REQUIRED,
    ACTION_PROGRESS_PLAN_CONFIRMED,
    ACTION_PROGRESS_PLAN_GENERATED,
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
    PROGRESS_TARGET_KIND,
    ProgressReporter,
    TemporalHeartbeatReporter,
    is_progress_action,
)
from j1.runs.store import IngestionRunStore, JsonlIngestionRunStore

__all__ = [
    "ACTION_PROGRESS_ASSESSMENT_COMPLETED",
    "ACTION_PROGRESS_ASSESSMENT_STARTED",
    "ACTION_PROGRESS_DOCUMENT_RECEIVED",
    "ACTION_PROGRESS_HUMAN_REVIEW_REQUIRED",
    "ACTION_PROGRESS_PLAN_CONFIRMED",
    "ACTION_PROGRESS_PLAN_GENERATED",
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
    "EXECUTION_DECISION_CONDITIONAL",
    "EXECUTION_DECISION_RUN",
    "EXECUTION_DECISION_SKIP",
    "IngestionRun",
    "IngestionRunStore",
    "JsonlIngestionRunStore",
    "NoopProgressReporter",
    "PROGRESS_ACTION_PREFIX",
    "PROGRESS_SEVERITY_ERROR",
    "PROGRESS_SEVERITY_INFO",
    "PROGRESS_SEVERITY_WARNING",
    "PROGRESS_TARGET_KIND",
    "ProgressEvent",
    "ProgressReporter",
    "RunStatus",
    "TemporalHeartbeatReporter",
    "is_progress_action",
]
