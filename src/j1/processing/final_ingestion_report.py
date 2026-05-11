"""end-to-end final ingestion report.

The `final_ingestion_report` artifact is the operator-facing summary
of one ingestion run. It aggregates the pre-compile plan, compile
result, post-compile enrich plan, enrichment overlay, and the
workflow's terminal status into a single typed dict the FE can
render on the run-detail page without fanning out to N artifact
endpoints.

The report is the single source of truth for downstream consumers
(audit dashboards, the FE state machine, the operator CLI). per-artifact endpoints; this aggregate on
top — pre- runs return `"unavailable"` from the read service
so the FE falls back to the per-artifact endpoints.

Builders here are PURE — they take the inputs the workflow already
has (`IngestionRun`, persisted artifact payloads, the final
status projection) and emit the typed report. No I/O. Same inputs →
same payload.

Vocabulary:
 * `final_status` — `INGESTION_STATUS_*` literal.
 * `final_status_reason` — operator-readable one-line explanation.
 * `stages[]` — fixed ordered list of `StageSummary`
 entries the FE renders as a timeline.
 * `compile_summary` — denormalised typed view over the
 compile-result artifact (so the FE
 can render the headline numbers from
 one fetch).
 * `enrichment_summary` — denormalised typed view over the
 enrichment-result artifact.
 * `artifact_refs` — dict of artifact-kind → artifact_id
 so the FE can deep-link to the
 detailed per-artifact endpoints.

Forbidden vocabulary: this module MUST NOT mention legacy
pre-compile gating concepts. Tests in
`test_final_ingestion_report.py` enforce this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from j1.processing.final_status import (
    INGESTION_STATUS_CANCELLED,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
    INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
    INGESTION_STATUS_FAILED_COMPILE,
    INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
    INGESTION_STATUS_FAILED_FINALIZATION,
    INGESTION_STATUS_FAILED_UNKNOWN,
    project_final_status,
)


__all__ = [
    "FINAL_INGESTION_REPORT_SCHEMA_VERSION",
    "STAGE_ID_ASSESSMENT",
    "STAGE_ID_COMPILE",
    "STAGE_ID_COMPILE_RESULT_NORMALIZATION",
    "STAGE_ID_POST_COMPILE_ANALYSIS",
    "STAGE_ID_ENRICHMENT",
    "STAGE_ID_FINALIZATION",
    "REQUIRED_STAGE_IDS",
    "STAGE_STATUS_PENDING",
    "STAGE_STATUS_SKIPPED",
    "STAGE_STATUS_RUNNING",
    "STAGE_STATUS_SUCCEEDED",
    "STAGE_STATUS_SUCCEEDED_WITH_WARNINGS",
    "STAGE_STATUS_FAILED",
    "StageSummary",
    "CompileSummary",
    "EnrichmentSummary",
    "FinalIngestionReport",
    "build_final_ingestion_report",
    "ReportSourceInputs",
]


FINAL_INGESTION_REPORT_SCHEMA_VERSION = "1.0"


# ---- Stage identifiers ------------------------------------------

STAGE_ID_ASSESSMENT = "assessment"
STAGE_ID_COMPILE = "compile"
STAGE_ID_COMPILE_RESULT_NORMALIZATION = "compile_result_normalization"
STAGE_ID_POST_COMPILE_ANALYSIS = "post_compile_analysis"
STAGE_ID_ENRICHMENT = "enrichment"
STAGE_ID_FINALIZATION = "finalization"


REQUIRED_STAGE_IDS: tuple[str, ...] = (
    STAGE_ID_ASSESSMENT,
    STAGE_ID_COMPILE,
    STAGE_ID_COMPILE_RESULT_NORMALIZATION,
    STAGE_ID_POST_COMPILE_ANALYSIS,
    STAGE_ID_ENRICHMENT,
    STAGE_ID_FINALIZATION,
)


_STAGE_LABELS: dict[str, str] = {
    STAGE_ID_ASSESSMENT: "Preparing document",
    STAGE_ID_COMPILE: "Base compile",
    STAGE_ID_COMPILE_RESULT_NORMALIZATION: "Compile result summary",
    STAGE_ID_POST_COMPILE_ANALYSIS: "Compile quality analysis",
    STAGE_ID_ENRICHMENT: "Domain enrichment",
    STAGE_ID_FINALIZATION: "Finalize",
}


# ---- Stage status vocabulary ------------------------------------

STAGE_STATUS_PENDING = "pending"
STAGE_STATUS_SKIPPED = "skipped"
STAGE_STATUS_RUNNING = "running"
STAGE_STATUS_SUCCEEDED = "succeeded"
STAGE_STATUS_SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
STAGE_STATUS_FAILED = "failed"


_VALID_STAGE_STATUSES: frozenset[str] = frozenset({
    STAGE_STATUS_PENDING,
    STAGE_STATUS_SKIPPED,
    STAGE_STATUS_RUNNING,
    STAGE_STATUS_SUCCEEDED,
    STAGE_STATUS_SUCCEEDED_WITH_WARNINGS,
    STAGE_STATUS_FAILED,
})


@dataclass(frozen=True)
class StageSummary:
    """One stage's terminal state in the report timeline.

 Pure data. The FE renders this as a row on the pipeline
 timeline; the operator CLI / audit log reads the same shape.

 `artifact_refs` carries kind→artifact_id pointers for any
 artifacts produced by this stage so a downstream consumer can
 deep-link without re-resolving the artifact registry."""

    stage_id: str
    label: str
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    artifact_refs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "artifact_refs": dict(self.artifact_refs),
        }


@dataclass(frozen=True)
class CompileSummary:
    """Denormalised compile-stage signals. The FE reads these
 instead of re-fetching the compile-result artifact for the
 headline numbers on the run-detail page."""

    compile_engine: str | None = None
    compile_status: str | None = None
    chunks_count: int = 0
    page_count: int | None = None
    extracted_text_chars: int | None = None
    detected_tables_count: int = 0
    detected_images_count: int = 0
    quality_verdict: str | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    retry_count: int = 0
    artifact_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "compile_engine": self.compile_engine,
            "compile_status": self.compile_status,
            "chunks_count": self.chunks_count,
            "page_count": self.page_count,
            "extracted_text_chars": self.extracted_text_chars,
            "detected_tables_count": self.detected_tables_count,
            "detected_images_count": self.detected_images_count,
            "quality_verdict": self.quality_verdict,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "retry_count": self.retry_count,
            "artifact_refs": list(self.artifact_refs),
        }


@dataclass(frozen=True)
class EnrichmentSummary:
    """Denormalised enrichment-overlay signals. The FE reads these
 instead of re-fetching the enrichment-result artifact for the
 headline numbers + skip-reason + module-count display."""

    should_enrich: bool = False
    enrichment_status: str | None = None  # succeeded / succeeded_with_warnings / failed / skipped / pending
    policy: str | None = None  # auto / always / never
    require_enrichment_success: bool = False
    selected_modules: tuple[str, ...] = ()
    skipped_modules: tuple[str, ...] = ()
    module_outcomes: tuple[dict[str, Any], ...] = ()
    # Operator-readable one-line claims about what enrichment
    # actually produced (e.g. "Document metadata: 3 fields";
    # "Terminology entries: 12"). Empty when modules ran but
    # produced no output — the FE renders a neutral "no enrichment
    # artefacts" message rather than pretending.
    what_enrichment_added: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    retry_count: int = 0
    skipped_reason: str | None = None
    artifact_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_enrich": self.should_enrich,
            "enrichment_status": self.enrichment_status,
            "policy": self.policy,
            "require_enrichment_success": self.require_enrichment_success,
            "selected_modules": list(self.selected_modules),
            "skipped_modules": list(self.skipped_modules),
            "module_outcomes": [dict(o) for o in self.module_outcomes],
            "what_enrichment_added": list(self.what_enrichment_added),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "retry_count": self.retry_count,
            "skipped_reason": self.skipped_reason,
            "artifact_refs": list(self.artifact_refs),
        }


@dataclass(frozen=True)
class FinalIngestionReport:
    """The end-to-end report. Pure data. Persisted as the
 `final_ingestion_report` artifact at workflow terminal."""

    schema_version: str
    run_id: str
    document_id: str | None
    document_name: str | None
    tenant_id: str | None
    project_id: str | None
    domain_profile_id: str | None
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None
    final_status: str
    final_status_reason: str
    stages: tuple[StageSummary, ...]
    compile_summary: CompileSummary
    enrichment_summary: EnrichmentSummary
    artifact_refs: dict[str, str]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    retry_counts: dict[str, int]
    operator_notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "domain_profile_id": self.domain_profile_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "final_status": self.final_status,
            "final_status_reason": self.final_status_reason,
            "stages": [s.to_dict() for s in self.stages],
            "compile_summary": self.compile_summary.to_dict(),
            "enrichment_summary": self.enrichment_summary.to_dict(),
            "artifact_refs": dict(self.artifact_refs),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "retry_counts": dict(self.retry_counts),
            "operator_notes": list(self.operator_notes),
        }


# ---- Inputs to the builder --------------------------------------


@dataclass(frozen=True)
class ReportSourceInputs:
    """Everything the workflow has at terminal that the builder
 needs to produce a `FinalIngestionReport`. Plain dicts so the
 workflow can hand persisted artifact payloads through unchanged."""

    run_id: str
    document_id: str | None
    document_name: str | None
    tenant_id: str | None
    project_id: str | None
    started_at: str | None
    completed_at: str | None
    framework_final_status: str  # `completed` / `partial_completed` / `failed` / `cancelled` / `timed_out`
    failure_code: str | None
    failure_message: str | None
    warning_count: int = 0
    # Persisted artifact payloads (already pre-fetched by the
    # workflow / activity). Missing keys are tolerated — the
    # builder fills the stage as `pending` and the summary fields
    # with safe defaults.
    initial_execution_plan: dict[str, Any] | None = None
    compile_result_summary: dict[str, Any] | None = None
    post_compile_enrich_plan: dict[str, Any] | None = None
    enrichment_result: dict[str, Any] | None = None
    final_summary: dict[str, Any] | None = None
    # Artifact-id pointers for deep-linking.
    artifact_refs: dict[str, str] = field(default_factory=dict)
    # Raw compile artifact refs (`raw_artifact_refs` from
    # `NormalizedCompileResult`) — surface so operators can locate
    # the preserved vendor output.
    raw_compile_artifact_refs: tuple[str, ...] = ()
    # Operator-supplied notes; empty by default. doesn't
    # provide a write surface for these; reserved for.
    operator_notes: tuple[str, ...] = ()


# ---- Builder -----------------------------------------------------


def build_final_ingestion_report(
    inputs: ReportSourceInputs,
) -> FinalIngestionReport:
    """Project the workflow's terminal-time state onto the typed
 report. Pure. Same inputs → same output."""

    duration_ms = _compute_duration_ms(inputs.started_at, inputs.completed_at)

    enrichment_status = _enrichment_status_from_payload(
        inputs.enrichment_result,
    )
    enrichment_skipped_reason = _enrichment_skipped_reason_from_payload(
        inputs.enrichment_result, inputs.post_compile_enrich_plan,
    )
    enrichment_required = _enrichment_required_from_payloads(
        inputs.initial_execution_plan, inputs.post_compile_enrich_plan,
    )

    projection = project_final_status(
        framework_final_status=inputs.framework_final_status,
        failure_code=inputs.failure_code,
        enrichment_status=enrichment_status,
        enrichment_required=enrichment_required,
        enrichment_skipped_reason=enrichment_skipped_reason,
    )

    compile_summary = _build_compile_summary(inputs.compile_result_summary)
    enrichment_summary = _build_enrichment_summary(
        inputs.post_compile_enrich_plan,
        inputs.enrichment_result,
        inputs.initial_execution_plan,
    )

    stages = _build_stages(
        inputs=inputs,
        final_status=projection.status,
        compile_summary=compile_summary,
        enrichment_summary=enrichment_summary,
    )

    # Aggregate retry counts. Enrichment retry is reserved for
    # future limiter accounting (currently always 0).
    retry_counts: dict[str, int] = {
        "compile": compile_summary.retry_count,
        "enrichment": enrichment_summary.retry_count,
    }

    # Aggregate warnings + errors across stages so the report's
    # top-level lists are the single source of truth for the FE
    # banner row.
    all_warnings: list[str] = []
    all_errors: list[str] = []
    for s in stages:
        all_warnings.extend(s.warnings)
        all_errors.extend(s.errors)

    artifact_refs = dict(inputs.artifact_refs)
    if inputs.raw_compile_artifact_refs:
        artifact_refs["raw_compile_artifact_refs"] = ", ".join(
            inputs.raw_compile_artifact_refs,
        )

    domain_profile_id = _domain_profile_from_payloads(
        inputs.initial_execution_plan,
    )

    return FinalIngestionReport(
        schema_version=FINAL_INGESTION_REPORT_SCHEMA_VERSION,
        run_id=inputs.run_id,
        document_id=inputs.document_id,
        document_name=inputs.document_name,
        tenant_id=inputs.tenant_id,
        project_id=inputs.project_id,
        domain_profile_id=domain_profile_id,
        started_at=inputs.started_at,
        completed_at=inputs.completed_at,
        duration_ms=duration_ms,
        final_status=projection.status,
        final_status_reason=projection.reason,
        stages=stages,
        compile_summary=compile_summary,
        enrichment_summary=enrichment_summary,
        artifact_refs=artifact_refs,
        warnings=tuple(all_warnings),
        errors=tuple(all_errors),
        retry_counts=retry_counts,
        operator_notes=inputs.operator_notes,
    )


# ---- Helpers ----------------------------------------------------


def _compute_duration_ms(
    started_at: str | None, completed_at: str | None,
) -> int | None:
    if not started_at or not completed_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = end - start
    return max(0, int(delta.total_seconds() * 1000))


def _enrichment_status_from_payload(
    payload: dict[str, Any] | None,
) -> str | None:
    if not payload:
        return None
    status = payload.get("status")
    return str(status) if status else None


def _enrichment_skipped_reason_from_payload(
    enrichment_payload: dict[str, Any] | None,
    enrich_plan_payload: dict[str, Any] | None,
) -> str | None:
    if enrichment_payload and enrichment_payload.get("status") == "skipped":
        # `EnrichmentResult` serialises the skip reason
        # under `skipped_reason`; fall back to `reason` for older
        # payloads that pre-date the rename.
        reason = (
            enrichment_payload.get("skipped_reason")
            or enrichment_payload.get("reason")
        )
        if reason:
            return str(reason)
    if enrich_plan_payload:
        # Plan-side skipped — when the assessor said "no enrichment".
        if enrich_plan_payload.get("should_enrich") is False:
            reasons = enrich_plan_payload.get("reasons") or []
            if reasons:
                return "; ".join(str(r) for r in reasons[:2])
    return None


def _enrichment_required_from_payloads(
    initial_plan: dict[str, Any] | None,
    enrich_plan: dict[str, Any] | None,
) -> bool:
    # Post-compile plan wins when present (resolved per-run); fall
    # back to the initial plan's policy reflection.
    if enrich_plan and "require_enrichment_success" in enrich_plan:
        return bool(enrich_plan.get("require_enrichment_success"))
    if initial_plan and "require_enrichment_success" in initial_plan:
        return bool(initial_plan.get("require_enrichment_success"))
    return False


def _domain_profile_from_payloads(
    initial_plan: dict[str, Any] | None,
) -> str | None:
    if not initial_plan:
        return None
    val = initial_plan.get("domain_profile_id")
    return str(val) if val else None


def _build_compile_summary(
    payload: dict[str, Any] | None,
) -> CompileSummary:
    if not payload:
        return CompileSummary()
    retries = payload.get("retry_attempts") or []
    retry_count = max(0, len(retries) - 1) if isinstance(retries, list) else 0
    return CompileSummary(
        compile_engine=_optional_str(payload.get("parser")),
        compile_status=_optional_str(payload.get("status")),
        chunks_count=int(payload.get("chunks_count") or 0),
        page_count=(
            int(payload["page_count"])
            if isinstance(payload.get("page_count"), int)
            else None
        ),
        extracted_text_chars=(
            int(payload["extracted_text_chars"])
            if isinstance(payload.get("extracted_text_chars"), int)
            else None
        ),
        detected_tables_count=len(payload.get("detected_tables") or []),
        detected_images_count=len(payload.get("detected_images") or []),
        quality_verdict=_optional_str(payload.get("final_quality")),
        warnings=tuple(str(w) for w in (payload.get("warnings") or [])),
        errors=tuple(str(e) for e in (payload.get("errors") or [])),
        retry_count=retry_count,
        artifact_refs=tuple(
            str(r) for r in (payload.get("raw_artifact_refs") or [])
        ),
    )


def _build_enrichment_summary(
    enrich_plan: dict[str, Any] | None,
    enrichment_result: dict[str, Any] | None,
    initial_plan: dict[str, Any] | None,
) -> EnrichmentSummary:
    should_enrich = bool(
        enrich_plan and enrich_plan.get("should_enrich")
    )
    policy = (
        _optional_str(initial_plan.get("enrichment_policy"))
        if initial_plan else None
    )
    require_success = _enrichment_required_from_payloads(
        initial_plan, enrich_plan,
    )

    enrichment_status = _enrichment_status_from_payload(enrichment_result)
    selected = tuple(
        str(t) for t in (
            (enrich_plan or {}).get("recommended_tasks") or []
        )
    )
    skipped = tuple(
        str(t) for t in (
            (enrich_plan or {}).get("skipped_tasks") or []
        )
    )

    outcomes: tuple[dict[str, Any], ...] = ()
    additions: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    skipped_reason: str | None = None
    refs: tuple[str, ...] = ()

    if enrichment_result:
        raw_outcomes = enrichment_result.get("module_outcomes") or []
        outcomes = tuple(dict(o) for o in raw_outcomes if isinstance(o, dict))
        warnings = [str(w) for w in (enrichment_result.get("warnings") or [])]
        errors = [str(e) for e in (enrichment_result.get("errors") or [])]
        if enrichment_status == "skipped":
            # the enrichment payload exposes the
            # operator-readable reason under `skipped_reason` (the
            # `EnrichmentResult` schema's serialised field). Older
            # paths used `reason` — keep both readable so a payload
            # produced before the rename still surfaces the reason.
            reason = (
                enrichment_result.get("skipped_reason")
                or enrichment_result.get("reason")
            )
            if reason:
                skipped_reason = str(reason)
        meta = enrichment_result.get("document_metadata") or {}
        terms = enrichment_result.get("terminology") or []
        if isinstance(meta, dict) and meta:
            additions.append(
                f"Document metadata: {len(meta)} "
                f"field{'s' if len(meta) != 1 else ''}",
            )
        if isinstance(terms, list) and terms:
            additions.append(f"Terminology entries: {len(terms)}")
        refs = tuple(
            str(r) for o in outcomes
            for r in (o.get("output_artifact_refs") or [])
        )
    elif enrich_plan and enrich_plan.get("should_enrich") is False:
        reasons = enrich_plan.get("reasons") or []
        if reasons:
            skipped_reason = "; ".join(str(r) for r in reasons[:2])

    return EnrichmentSummary(
        should_enrich=should_enrich,
        enrichment_status=enrichment_status,
        policy=policy,
        require_enrichment_success=require_success,
        selected_modules=selected,
        skipped_modules=skipped,
        module_outcomes=outcomes,
        what_enrichment_added=tuple(additions),
        warnings=tuple(warnings),
        errors=tuple(errors),
        retry_count=0,
        skipped_reason=skipped_reason,
        artifact_refs=refs,
    )


def _build_stages(
    *,
    inputs: ReportSourceInputs,
    final_status: str,
    compile_summary: CompileSummary,
    enrichment_summary: EnrichmentSummary,
) -> tuple[StageSummary, ...]:
    """Build the 6 fixed stage summaries.

 Status inference is conservative — each stage transitions
 PENDING → RUNNING → SUCCEEDED / SUCCEEDED_WITH_WARNINGS /
 FAILED / SKIPPED based on what the upstream artifacts tell us.
 Missing artifacts → stage stays PENDING (with `reasons` carrying
 the "not produced" explanation)."""

    stages: list[StageSummary] = []

    # ---- Assessment (pre-compile) ----
    assessment_status = (
        STAGE_STATUS_SUCCEEDED
        if inputs.initial_execution_plan
        else STAGE_STATUS_PENDING
    )
    assessment_reasons: tuple[str, ...] = ()
    if inputs.initial_execution_plan:
        plan_warnings = inputs.initial_execution_plan.get("warnings") or []
        if plan_warnings:
            assessment_status = STAGE_STATUS_SUCCEEDED_WITH_WARNINGS
    else:
        assessment_reasons = ("initial execution plan not produced",)
    stages.append(StageSummary(
        stage_id=STAGE_ID_ASSESSMENT,
        label=_STAGE_LABELS[STAGE_ID_ASSESSMENT],
        status=assessment_status,
        reasons=assessment_reasons,
        warnings=tuple(
            str(w) for w in (
                (inputs.initial_execution_plan or {}).get("warnings") or []
            )
        ),
        artifact_refs=_pluck_refs(
            inputs.artifact_refs, "initial_execution_plan",
        ),
    ))

    # ---- Compile ----
    compile_failed = final_status == INGESTION_STATUS_FAILED_COMPILE
    if compile_failed:
        compile_stage_status = STAGE_STATUS_FAILED
    elif compile_summary.compile_status == "succeeded":
        compile_stage_status = (
            STAGE_STATUS_SUCCEEDED_WITH_WARNINGS
            if compile_summary.warnings
            else STAGE_STATUS_SUCCEEDED
        )
    elif inputs.compile_result_summary:
        compile_stage_status = STAGE_STATUS_SUCCEEDED
    else:
        compile_stage_status = STAGE_STATUS_PENDING
    stages.append(StageSummary(
        stage_id=STAGE_ID_COMPILE,
        label=_STAGE_LABELS[STAGE_ID_COMPILE],
        status=compile_stage_status,
        warnings=compile_summary.warnings,
        errors=(
            (inputs.failure_message,)
            if compile_failed and inputs.failure_message
            else ()
        ),
        artifact_refs=_pluck_refs(inputs.artifact_refs, "compile"),
    ))

    # ---- Compile result normalization ----
    normalization_status = (
        STAGE_STATUS_SUCCEEDED
        if inputs.compile_result_summary
        else (STAGE_STATUS_PENDING if compile_failed else STAGE_STATUS_PENDING)
    )
    stages.append(StageSummary(
        stage_id=STAGE_ID_COMPILE_RESULT_NORMALIZATION,
        label=_STAGE_LABELS[STAGE_ID_COMPILE_RESULT_NORMALIZATION],
        status=normalization_status,
        reasons=(
            ("compile_result_summary not produced — compile failed first",)
            if compile_failed and not inputs.compile_result_summary
            else ()
        ),
        artifact_refs=_pluck_refs(
            inputs.artifact_refs, "compile_result_summary",
        ),
    ))

    # ---- Post-compile analysis ----
    if inputs.post_compile_enrich_plan:
        analysis_status = STAGE_STATUS_SUCCEEDED
    elif compile_failed:
        analysis_status = STAGE_STATUS_SKIPPED
    else:
        analysis_status = STAGE_STATUS_PENDING
    analysis_reasons: tuple[str, ...] = ()
    if analysis_status == STAGE_STATUS_SKIPPED and compile_failed:
        analysis_reasons = (
            "compile failed; no compile output to analyse",
        )
    stages.append(StageSummary(
        stage_id=STAGE_ID_POST_COMPILE_ANALYSIS,
        label=_STAGE_LABELS[STAGE_ID_POST_COMPILE_ANALYSIS],
        status=analysis_status,
        reasons=analysis_reasons,
        artifact_refs=_pluck_refs(
            inputs.artifact_refs, "post_compile_enrich_plan",
        ),
    ))

    # ---- Enrichment ----
    enrichment_stage_status = _enrichment_stage_status(
        final_status=final_status,
        enrichment_status=enrichment_summary.enrichment_status,
        compile_failed=compile_failed,
    )
    enrichment_reasons: tuple[str, ...] = ()
    if enrichment_summary.skipped_reason:
        enrichment_reasons = (enrichment_summary.skipped_reason,)
    elif compile_failed:
        enrichment_reasons = ("compile failed; enrichment not attempted",)
    stages.append(StageSummary(
        stage_id=STAGE_ID_ENRICHMENT,
        label=_STAGE_LABELS[STAGE_ID_ENRICHMENT],
        status=enrichment_stage_status,
        reasons=enrichment_reasons,
        warnings=enrichment_summary.warnings,
        errors=enrichment_summary.errors,
        artifact_refs=_pluck_refs(inputs.artifact_refs, "enrichment_result"),
    ))

    # ---- Finalization ----
    finalize_status = _finalize_stage_status(
        final_status=final_status,
        final_summary_present=inputs.final_summary is not None,
    )
    finalize_errors: tuple[str, ...] = ()
    if final_status == INGESTION_STATUS_FAILED_FINALIZATION:
        finalize_errors = tuple(
            v for v in (inputs.failure_message,) if v
        )
    stages.append(StageSummary(
        stage_id=STAGE_ID_FINALIZATION,
        label=_STAGE_LABELS[STAGE_ID_FINALIZATION],
        status=finalize_status,
        errors=finalize_errors,
        artifact_refs=_pluck_refs(inputs.artifact_refs, "final_summary"),
    ))

    return tuple(stages)


def _enrichment_stage_status(
    *,
    final_status: str,
    enrichment_status: str | None,
    compile_failed: bool,
) -> str:
    if compile_failed:
        return STAGE_STATUS_SKIPPED
    if enrichment_status == "skipped":
        return STAGE_STATUS_SKIPPED
    if enrichment_status == "succeeded":
        return STAGE_STATUS_SUCCEEDED
    if enrichment_status == "succeeded_with_warnings":
        return STAGE_STATUS_SUCCEEDED_WITH_WARNINGS
    if enrichment_status == "failed":
        if final_status == INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED:
            return STAGE_STATUS_FAILED
        return STAGE_STATUS_SUCCEEDED_WITH_WARNINGS
    if final_status in (
        INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
        INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
    ):
        return STAGE_STATUS_SUCCEEDED
    if final_status == INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT:
        return STAGE_STATUS_SKIPPED
    return STAGE_STATUS_PENDING


def _finalize_stage_status(
    *,
    final_status: str,
    final_summary_present: bool,
) -> str:
    if final_status == INGESTION_STATUS_FAILED_FINALIZATION:
        return STAGE_STATUS_FAILED
    if final_status in (
        INGESTION_STATUS_FAILED_COMPILE,
        INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
        INGESTION_STATUS_FAILED_UNKNOWN,
        INGESTION_STATUS_CANCELLED,
    ):
        return STAGE_STATUS_SKIPPED
    if final_summary_present:
        return STAGE_STATUS_SUCCEEDED
    if final_status in (
        INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
        INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
        INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
    ):
        return STAGE_STATUS_SUCCEEDED
    return STAGE_STATUS_PENDING


def _pluck_refs(
    artifact_refs: dict[str, str], *keys: str,
) -> dict[str, str]:
    """Return a sub-dict of artifact_refs with the keys that exist.
 Empty when none of the keys are present."""
    return {k: artifact_refs[k] for k in keys if k in artifact_refs}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
