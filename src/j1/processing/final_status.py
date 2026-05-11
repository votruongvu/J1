"""final ingestion status vocabulary.

The existing `FinalStatus` enum (`completed` / `partial_completed`
/ `failed`) is the framework-internal verdict. a
finer-grained OPERATOR-FACING status vocabulary so the FE +
audit log can distinguish "completed without enrichment" from
"completed with enrichment warnings" from "failed because
required enrichment didn't complete".

Both layers coexist:
 * Workflow code still returns the internal `FinalStatus` enum.
 * Final-report consumers (FE, audit, run-detail page) project
 onto the new `IngestionFinalStatus` via
 `project_final_status`.

The projection reads the new status from explicit signals in the
run state:
 * compile failure → `failed_compile`
 * required enrichment failure → `failed_enrichment_required`
 * enrichment skipped + run otherwise clean → `completed_without_enrichment`
 * enrichment ran with warnings or partial failures (and policy
 didn't require success) → `completed_with_enrichment_warnings`
 * enrichment ran cleanly → `completed_with_enrichment`
 * finalize itself failed → `failed_finalization`

Each value is a stable string the FE branches on. Adding a new
status is a coordinated FE + audit change; the projection helper
is the single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = [
    "INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT",
    "INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT",
    "INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS",
    "INGESTION_STATUS_FAILED_COMPILE",
    "INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED",
    "INGESTION_STATUS_FAILED_FINALIZATION",
    "INGESTION_STATUS_FAILED_UNKNOWN",
    "INGESTION_STATUS_CANCELLED",
    "ALL_INGESTION_FINAL_STATUSES",
    "IngestionFinalStatusProjection",
    "project_final_status",
]


# Stable wire vocabulary. The FE + audit-log consumers branch on
# these exact strings; renames are migrations. Keep paired with
# any FE-side enum copy.

INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT = "completed_without_enrichment"
INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT = "completed_with_enrichment"
INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS = (
    "completed_with_enrichment_warnings"
)
INGESTION_STATUS_FAILED_COMPILE = "failed_compile"
INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED = "failed_enrichment_required"
INGESTION_STATUS_FAILED_FINALIZATION = "failed_finalization"
INGESTION_STATUS_FAILED_UNKNOWN = "failed"
INGESTION_STATUS_CANCELLED = "cancelled"


ALL_INGESTION_FINAL_STATUSES: tuple[str, ...] = (
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
    INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
    INGESTION_STATUS_FAILED_COMPILE,
    INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
    INGESTION_STATUS_FAILED_FINALIZATION,
    INGESTION_STATUS_FAILED_UNKNOWN,
    INGESTION_STATUS_CANCELLED,
)


@dataclass(frozen=True)
class IngestionFinalStatusProjection:
    """The projected status for one run.

 `status` is one of the `INGESTION_STATUS_*` literals. `reason`
 is an operator-readable one-liner the FE can render alongside
 the badge ("enrichment skipped: domain policy=never")."""

    status: str
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


def project_final_status(
    *,
    framework_final_status: str,
    failure_code: str | None = None,
    enrichment_status: str | None = None,
    enrichment_required: bool = False,
    enrichment_skipped_reason: str | None = None,
) -> IngestionFinalStatusProjection:
    """Project the framework's `FinalStatus` + structured signals
 onto the operator-facing status vocabulary.

 Inputs:
 * `framework_final_status` — `FinalStatus.value` from
 `ProjectProcessingResult` (`completed` / `partial_completed`
 / `failed` / `cancelled` / `timed_out`).
 * `failure_code` — `IngestionRun.failure_code` (e.g.
 `ENRICHMENT_REQUIRED`, `COMPILE_FAILED`). Drives the
 failed-* projection when present.
 * `enrichment_status` — from the persisted enrichment_result
 artifact (`succeeded` / `succeeded_with_warnings` /
 `failed` / `skipped`). Drives the completed-* projection
 when the framework status is success-ish.
 * `enrichment_required` — the resolved
 `require_enrichment_success` for this run. Tags a failed
 enrichment as the run-failing reason when True.
 * `enrichment_skipped_reason` — for the
 `completed_without_enrichment` reason text.

 Pure — no I/O. Same inputs → same projection."""
    if framework_final_status == "cancelled":
        return IngestionFinalStatusProjection(
            status=INGESTION_STATUS_CANCELLED,
            reason="run was cancelled by operator",
        )

    # Failure paths first — fail-codes are precise; let them win
    # over the coarse framework status.
    if failure_code:
        code = failure_code.upper()
        if code == "ENRICHMENT_REQUIRED":
            return IngestionFinalStatusProjection(
                status=INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
                reason=(
                    "required enrichment did not complete; raw compile "
                    "output is preserved"
                ),
            )
        if code in (
            "COMPILE_FAILED", "CHUNK_FAILED", "INDEX_FAILED",
            "VERIFICATION_FAILED", "EMPTY_DOCUMENT",
        ):
            return IngestionFinalStatusProjection(
                status=INGESTION_STATUS_FAILED_COMPILE,
                reason=f"compile-stage failure: {failure_code}",
            )
        if code == "FINALIZATION_FAILED":
            return IngestionFinalStatusProjection(
                status=INGESTION_STATUS_FAILED_FINALIZATION,
                reason="finalize step failed after a successful pipeline",
            )

    if framework_final_status == "failed":
        return IngestionFinalStatusProjection(
            status=INGESTION_STATUS_FAILED_UNKNOWN,
            reason=(
                "run failed without a specific failure code; "
                "inspect error_report"
            ),
        )

    # Success-ish paths. The framework reports `completed` or
    # `partial_completed`; we refine using enrichment signals.
    if enrichment_status == "skipped":
        reason = (
            enrichment_skipped_reason
            or "enrichment skipped by post-compile assessor"
        )
        return IngestionFinalStatusProjection(
            status=INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
            reason=reason,
        )

    if enrichment_status == "failed" and not enrichment_required:
        return IngestionFinalStatusProjection(
            status=INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
            reason=(
                "enrichment failed but policy didn't require success; "
                "compile output remains usable"
            ),
        )

    if (
        enrichment_status == "succeeded_with_warnings"
        or framework_final_status == "partial_completed"
    ):
        return IngestionFinalStatusProjection(
            status=INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
            reason="enrichment ran with warnings",
        )

    if enrichment_status == "succeeded":
        return IngestionFinalStatusProjection(
            status=INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
            reason="enrichment overlay produced",
        )

    if framework_final_status == "completed":
        # Completed with no enrichment signals at all — typically
        # legacy runs persisted or runs where the
        # enrichment activity wasn't dispatched.
        return IngestionFinalStatusProjection(
            status=INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
            reason="no enrichment_result produced for this run",
        )

    # Fallthrough — shouldn't reach in practice, but the FE
    # consumer expects a string, so default to failed_unknown.
    return IngestionFinalStatusProjection(
        status=INGESTION_STATUS_FAILED_UNKNOWN,
        reason=f"unrecognised framework status: {framework_final_status!r}",
    )
