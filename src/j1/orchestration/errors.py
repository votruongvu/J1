"""Stable Temporal `ApplicationError.type` strings for ingestion failures.

These appear as the `type` field on the Temporal failure UI. Operators
filter on them to distinguish ingestion-level failures from
infrastructure noise; the worker's retry policy uses them to decide
whether to retry. Keep them stable across releases — changing a
constant here will silently invalidate operator dashboards and may
flip a previously-non-retryable error back to retryable.

Convention: `J1_INGEST_<UPPER_SNAKE>`. Add new types only when the
existing set can't carry the meaning."""

from __future__ import annotations

# A required ingestion step (compile, enrich-when-explicitly-requested,
# index, profiler, planner, etc.) reported a terminal failure. Raised
# by the workflow when an activity returned `status="failed"` for a
# stage the workflow can't continue without. `non_retryable=True`.
ERROR_TYPE_REQUIRED_STEP_FAILED = "J1_INGEST_REQUIRED_STEP_FAILED"

# An unexpected exception escaped the workflow's stage-level handlers.
# Wrapped here so Temporal UI shows a clean type. `non_retryable=False`
# — these are typically transient infrastructure issues (network blip,
# DB reconnect) and a parent workflow could legitimately retry.
ERROR_TYPE_UNEXPECTED_ERROR = "J1_INGEST_UNEXPECTED_ERROR"

# A request named an entity that doesn't exist: a document_id with no
# matching record, an artifact_id with no matching record, or a
# processor `kind` that isn't registered. Always `non_retryable=True`
# — re-running the same activity with the same input will fail
# identically. Caller-side bug; surface immediately.
ERROR_TYPE_LOOKUP_FAILED = "J1_INGEST_LOOKUP_FAILED"

__all__ = [
    "ERROR_TYPE_LOOKUP_FAILED",
    "ERROR_TYPE_REQUIRED_STEP_FAILED",
    "ERROR_TYPE_UNEXPECTED_ERROR",
]
