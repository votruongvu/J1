"""Resume-from-checkpoint helpers — settings hash + snapshot helpers.

A resume snapshot is captured at the workflow's terminal transition
(see `RunsActivities._persist_run_terminal`) and stored on
`IngestionRun.metadata["resume_snapshot"]`. The snapshot is the input
to `IngestionReviewService.resume_from_checkpoint`, which validates
compatibility before dispatching a new run that carries forward the
previously-produced artifacts and skips the LLM-cost stages that
already completed.

Field hygiene: the snapshot stores operational metadata only — never
document content, prompts, or model outputs. Settings snapshot fields
are processor kinds and policy flags, all of which are operator-set.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

__all__ = [
    "RESUME_SETTINGS_FIELDS",
    "RESUMABLE_STAGES",
    "build_resume_snapshot",
    "build_settings_snapshot",
    "compatible_settings",
    "compute_settings_hash",
    "settings_diff",
]

# The fields that materially change pipeline behaviour for the same
# document. If any of these differ between the original run and the
# resume request, the resume is rejected with a 412 — the operator
# must full-reindex instead. Keep this list narrow: anything that's
# purely operational (actor, correlation_id, search_attributes_enabled)
# does NOT belong here, because changing it shouldn't invalidate
# carry-forward state.
RESUME_SETTINGS_FIELDS: tuple[str, ...] = (
    "compiler_kind",
    "enricher_kind",
    "graph_builder_kind",
    "indexer_kind",
    "planner_enabled",
    "policy",
    "domain_override",
    "workspace_default_domain",
    "failure_policy",
)

# Step names that resume is allowed to elide when they were COMPLETED
# in the prior run. Compile + chunk-generation always re-run because
# their outputs are the structural backbone — every downstream stage
# reads them. Enrich + graph are the LLM-cost stages where re-running
# is expensive and the carry-forward is genuinely useful.
#
# These are STEP NAMES (the `step` field on `StepResult`), not
# Temporal activity names. The corresponding activities are
# `enrich` and `build_graph` respectively, but the workflow records
# them under steps `enrich` and `graph`.
RESUMABLE_STAGES: frozenset[str] = frozenset({"enrich", "graph"})


def _normalise(value: Any) -> Any:
    """Coerce enum / dataclass values to JSON-friendly primitives so
 the hash is stable across StrEnum vs str representations."""
    if value is None:
        return None
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return _normalise(value.value)
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def build_settings_snapshot(request: Any) -> dict[str, Any]:
    """Return the minimal dict that captures everything we hash for
 compatibility. `request` is anything with attribute access matching
 `RESUME_SETTINGS_FIELDS` (typically `ProjectProcessingRequest` or
 a duck-type test stub)."""
    return {
        field: _normalise(getattr(request, field, None))
        for field in RESUME_SETTINGS_FIELDS
    }


def compute_settings_hash(snapshot: Mapping[str, Any]) -> str:
    """SHA256 of the canonical settings snapshot. Sorted-key JSON so
 the hash is order-stable across Python dict iteration changes."""
    payload = json.dumps(
        {k: _normalise(v) for k, v in snapshot.items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compatible_settings(
    snapshot: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """True iff every field in `RESUME_SETTINGS_FIELDS` matches between
 snapshot (the prior run) and candidate (the proposed new run).
 Comparison runs through `_normalise` so a `StrEnum` vs a plain
 string compares equal."""
    return compute_settings_hash(snapshot) == compute_settings_hash(candidate)


def settings_diff(
    snapshot: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return `{field: {"before": x, "after": y}}` for every differing
 field. Empty dict iff the two snapshots are compatible. The
 operator-facing 412 response includes this diff so the caller can
 see why resume was rejected without guessing."""
    out: dict[str, dict[str, Any]] = {}
    for field in RESUME_SETTINGS_FIELDS:
        before = _normalise(snapshot.get(field))
        after = _normalise(candidate.get(field))
        if before != after:
            out[field] = {"before": before, "after": after}
    return out


def build_resume_snapshot(
    *,
    request: Any,
    step_results_payload: Iterable[Mapping[str, Any]],
    produced_artifact_ids: Iterable[str],
    produced_artifact_kinds: Iterable[str],
    failure_code: str | None = None,
    failure_message: str | None = None,
    snapshot_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the dict that gets stored on `IngestionRun.metadata
 ["resume_snapshot"]` at terminal transition. Pure — no I/O, no
 timezone surprises (defaults to UTC now)."""
    settings_snapshot = build_settings_snapshot(request)
    completed_steps: list[str] = []
    failed_steps: list[str] = []
    for entry in step_results_payload:
        status = str(entry.get("status") or "")
        step = str(entry.get("step") or "")
        if not step:
            continue
        if status == "completed":
            completed_steps.append(step)
        elif status == "failed":
            failed_steps.append(step)
    return {
        "settings_hash": compute_settings_hash(settings_snapshot),
        "settings_snapshot": settings_snapshot,
        "completed_steps": completed_steps,
        "failed_steps": failed_steps,
        "produced_artifact_ids": list(produced_artifact_ids),
        "produced_artifact_kinds": list(produced_artifact_kinds),
        "snapshot_at": (
            snapshot_at or datetime.now(timezone.utc)
        ).isoformat(),
        "failure_code": failure_code,
        "failure_message": failure_message,
    }
