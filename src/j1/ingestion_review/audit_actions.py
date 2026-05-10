"""Audit-event action constants for operator-initiated ingestion ops.

The `j1.ops.*` namespace tracks operator-driven mutations on ingestion
runs — distinct from `j1.progress.*` (workflow lifecycle: started,
completed, failed, etc.) and `j1.lifecycle.*` (Temporal-stage
transitions inside the workflow). When an operator clicks "Resume"
or "Purge" or uploads a multi-file batch, the REST endpoint records
one of these events so the audit log answers "who did what when"
without scraping workflow state.

Target kind for every ops event is the existing `"ingestion_run"`
(or `"ingestion_batch"` for batch dispatch). Payload carries the
specific action's parameters (e.g. resume's `resumed_steps` list,
purge's `artifacts_purged` count) — keep payload fields stable
across releases since the audit log is the historical record.

These constants are imported by:
  * `j1.adapters.rest.app` — emit at the operator boundary.
  * `tests/test_*.py` — assert on action strings.
"""

from __future__ import annotations

OPS_ACTION_PREFIX = "j1.ops."

# ---- Run-scoped ops --------------------------------------------------

ACTION_OPS_RUN_DELETED = OPS_ACTION_PREFIX + "run.deleted"
"""Soft-delete: operator tombstoned a run. Payload carries
`tombstoned_artifact_count` and `was_already_deleted`."""

ACTION_OPS_RUN_PURGED = OPS_ACTION_PREFIX + "run.purged"
"""Hard-delete: operator physically removed a run + artifacts.
Payload carries `artifacts_purged`, `files_deleted`, `files_missing`,
`snapshots_removed`, and validation cascade counts."""

ACTION_OPS_RUN_RESUMED = OPS_ACTION_PREFIX + "run.resumed"
"""Operator started a resume-from-checkpoint. Payload carries the
`original_run_id`, `resumed_steps` list, and `carry_forward_artifact_count`.
The new `run_id` is the audit event's `target_id`; the prior one is in
the payload."""

ACTION_OPS_RUN_REINDEXED = OPS_ACTION_PREFIX + "run.reindexed"
"""Operator started a full re-index (compile from source). Payload
carries `original_run_id` + `document_id`."""

ACTION_OPS_RUN_INDEX_REBUILT = OPS_ACTION_PREFIX + "run.index_rebuilt"
"""Operator started a rebuild-index-only (re-index existing chunks).
Payload carries `original_run_id`, `carry_forward_chunk_count`, and
`indexer_kind`."""

# ---- Batch-scoped ops ------------------------------------------------

ACTION_OPS_BATCH_DISPATCHED = OPS_ACTION_PREFIX + "batch.dispatched"
"""Operator uploaded a multi-file batch and the parent
`BatchOrchestrationWorkflow` was dispatched. Payload carries
`file_count`, `run_ids`, and `parent_workflow_id`. `target_id` is
the `batch_run_id`."""

# Target-kind constants — paired with action_constants so callers
# always use the right pair.
TARGET_KIND_INGESTION_RUN = "ingestion_run"
TARGET_KIND_INGESTION_BATCH = "ingestion_batch"

__all__ = [
    "ACTION_OPS_BATCH_DISPATCHED",
    "ACTION_OPS_RUN_DELETED",
    "ACTION_OPS_RUN_INDEX_REBUILT",
    "ACTION_OPS_RUN_PURGED",
    "ACTION_OPS_RUN_REINDEXED",
    "ACTION_OPS_RUN_RESUMED",
    "OPS_ACTION_PREFIX",
    "TARGET_KIND_INGESTION_BATCH",
    "TARGET_KIND_INGESTION_RUN",
]
