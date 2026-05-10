"""Stage-output validation contract.

Every durable stage in the ingestion pipeline (compile, generate_chunks,
enrich, graph) MUST go through this contract before being marked
`succeeded` on the workflow's `_step_results`. The core rule is:

    Never mark a stage succeeded just because a function returned
    successfully.

A stage is `succeeded` only when:

  1. Stage execution completed.
  2. Required output exists.
  3. Output is persisted.
  4. Output can be read back.
  5. Output passes quality checks.
  6. Output has correct tenant/project/workspace/run/document scope.
  7. Output links to the correct upstream input.
  8. Validation report is saved.
  9. Checkpoint is saved after validation passes.

Failure to satisfy any of these → stage is `failed`, the validation
errors are persisted, dependent downstream stages don't run, and
the workflow either raises or records the failure per its
`failure_policy`.

This module ONLY defines the contract (dataclasses + status
constants). The per-stage check functions live in
[`stage_validators.py`](./stage_validators.py); the activity that
runs them + persists the report lives in
[`activities/processing.py`](../orchestration/activities/processing.py)
under `validate_stage`.

Field hygiene: a `StageValidationResult` is logged + persisted as a
JSON artifact. Don't put document text, prompts, or model outputs
in `errors` / `warnings` / `checks[].message` — keep them as short
operational strings (capped at ~256 chars per check).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Validation status — three values mirror the per-stage outcome model
# the rest of the pipeline already uses (passed / warning / failed).
# `passed` = all required checks passed AND no warning checks failed
# in a way that policy considers blocking.
# `warning` = required checks passed but some non-blocking issue was
# surfaced (e.g. chunk count near the lower bound, missing optional
# scope field). The stage IS marked succeeded.
# `failed` = at least one required check failed. The stage MUST NOT
# be marked succeeded.
VALIDATION_STATUS_PASSED = "passed"
VALIDATION_STATUS_WARNING = "warning"
VALIDATION_STATUS_FAILED = "failed"

VALIDATION_STATUSES = frozenset({
    VALIDATION_STATUS_PASSED,
    VALIDATION_STATUS_WARNING,
    VALIDATION_STATUS_FAILED,
})

# Per-check status — narrower than the stage-level status because a
# single check is binary (pass/fail) plus an optional warning grade.
CHECK_STATUS_PASSED = "passed"
CHECK_STATUS_WARNING = "warning"
CHECK_STATUS_FAILED = "failed"

# Stage-name constants. Keep these in lockstep with the workflow's
# `_record_step` calls — drift here means the validator can't
# correlate against the right StepResult.
STAGE_COMPILE = "compile"
STAGE_GENERATE_CHUNKS = "generate_knowledge_chunks"
STAGE_ENRICH = "enrich"
STAGE_GRAPH = "graph"

# Aggregator stage name — the final-validation step that reads back
# every per-stage report and confirms cross-stage consistency
# (chunks match index, graph nodes reference real chunks, etc.).
STAGE_FINAL_VALIDATION = "final_validation"

# Validator schema version. Bumped when the StageValidationResult
# shape changes in a way that breaks backward compatibility (e.g.
# renaming a top-level field). Older persisted reports stay
# readable via the version field; new readers branch on it.
VALIDATOR_VERSION = "1"


@dataclass(frozen=True)
class StageValidationCheck:
    """One named check inside a stage's validation pass.

    `name` is a short snake_case identifier (e.g.
    `chunk_count_positive`, `chunk_ids_unique`) — operators grep
    these in audit logs to find runs that tripped a specific rule.
    `message` is a one-line operational string explaining the
    outcome; capped at 256 chars by the persist activity to keep
    JSON payloads small."""

    name: str
    status: str  # CHECK_STATUS_PASSED | CHECK_STATUS_WARNING | CHECK_STATUS_FAILED
    message: str | None = None


@dataclass(frozen=True)
class StageValidationResult:
    """The complete per-stage validation outcome.

    Persisted as a `stage_validation_report` artifact (one per stage
    per run). The workflow consults `validation_status` to decide
    whether to mark the step COMPLETED or FAILED.

    Most fields are operational scope identifiers; `checks` is the
    audit trail of which rules ran and `errors` / `warnings` are the
    flat lists operators read first when triaging. `output_refs` is
    the artifacts the stage produced; `artifact_refs` is what the
    validator read back to verify the stage. They overlap heavily
    today (`output_refs ⊂ artifact_refs` typically) but stay separate
    so future validators that consult upstream artifacts (e.g. graph
    validator reading chunk artifacts) can record them distinctly."""

    stage_name: str
    run_id: str
    document_id: str | None
    tenant_id: str
    project_id: str
    workspace_id: str | None
    attempt: int
    validation_status: str
    checks: list[StageValidationCheck] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    validator_version: str = VALIDATOR_VERSION

    def passed(self) -> bool:
        """True iff the stage may be marked succeeded. `warning`
        counts as passed (warnings are surfaced but non-blocking).
        Only `failed` blocks the COMPLETED transition."""
        return self.validation_status in (
            VALIDATION_STATUS_PASSED,
            VALIDATION_STATUS_WARNING,
        )

    def to_payload(self) -> dict[str, Any]:
        """JSON-serialisable shape for the `stage_validation_report`
        artifact. Stable across releases — readers may exist outside
        this codebase (audit dashboards, compliance exports). Bumping
        a field requires a `validator_version` increment."""
        return {
            "schema_version": self.validator_version,
            "stage_name": self.stage_name,
            "run_id": self.run_id,
            "document_id": self.document_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "attempt": self.attempt,
            "validation_status": self.validation_status,
            "checks": [
                {"name": c.name, "status": c.status, "message": c.message}
                for c in self.checks
            ],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "output_refs": list(self.output_refs),
            "artifact_refs": list(self.artifact_refs),
        }


def aggregate_status(checks: list[StageValidationCheck]) -> str:
    """Roll up a list of check outcomes into a stage-level status.

    Rules:
      * any `failed` check → `failed`.
      * else any `warning` check → `warning`.
      * else `passed`.

    Empty list = `passed` (a stage with no checks is trivially
    valid; this should be rare in practice — every durable stage
    has at least an artifact-existence check)."""
    has_warning = False
    for c in checks:
        if c.status == CHECK_STATUS_FAILED:
            return VALIDATION_STATUS_FAILED
        if c.status == CHECK_STATUS_WARNING:
            has_warning = True
    return (
        VALIDATION_STATUS_WARNING if has_warning
        else VALIDATION_STATUS_PASSED
    )


__all__ = [
    "CHECK_STATUS_FAILED",
    "CHECK_STATUS_PASSED",
    "CHECK_STATUS_WARNING",
    "STAGE_COMPILE",
    "STAGE_ENRICH",
    "STAGE_FINAL_VALIDATION",
    "STAGE_GENERATE_CHUNKS",
    "STAGE_GRAPH",
    "StageValidationCheck",
    "StageValidationResult",
    "VALIDATION_STATUS_FAILED",
    "VALIDATION_STATUS_PASSED",
    "VALIDATION_STATUS_WARNING",
    "VALIDATION_STATUSES",
    "VALIDATOR_VERSION",
    "aggregate_status",
]
