from dataclasses import dataclass, field
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.cost.breakdown import CostBreakdown, CostResult
from j1.jobs.status import ReviewStatus
from j1.processing.status import ResultStatus

# Canonical artifact-kind strings shared across providers, projectors,
# and the validation/review surface. Each kind names the contract a
# producer claims to satisfy; consumers (review projectors, retrieval,
# the FE Assets/Chunks/Graph tabs) match on these. Stable across
# releases — renaming silently breaks every consumer that reads them.
ARTIFACT_KIND_CHUNK = "chunk"
ARTIFACT_KIND_COMPILED_TEXT = "compiled.text"
# Stable parser-output boundary. The compile activity persists a
# normalized snapshot of post-parse stats (text/image/table/equation
# counts, page count, quality scores, per-image triage) under this
# kind so the post-compile replan + the FE quality surface can read
# it without re-walking the storage_dir. Independent of vendor
# internals — see `j1.processing.manifest` for the canonical schema.
ARTIFACT_KIND_PARSED_CONTENT_MANIFEST = "parsed_content_manifest"
# Raw parser output — the structured `content_list` the parser
# emits. Distinct from `parsed_content_manifest` (counts + items
# projection the FE renders); this kind is the literal vendor
# payload. Kept as a recognised artifact kind for stage-validation
# tolerance, but the current compile path produces it inline as
# part of `process_document_complete` rather than as a standalone
# artifact.
ARTIFACT_KIND_PARSED_SOURCE = "parsed_source"
# Post-compile Processing Plan artifact. Persisted by the planning
# activity once compile + content inventory are available; carries
# the rule-based assessment, the document-understanding summary, and
# (when LLM-assisted planning ran) the validated LLM output. Read by
# `/ingestion-runs/{id}/planning` and the FE Planning Report tab.
ARTIFACT_KIND_PLANNING_RESULT = "planning_result"
# Failure-path artifact. Written by the workflow's FAILED_FINAL
# handler so operators can inspect why a run failed via the same
# artifact-listing path that surfaces successful artifacts —
# instead of having to grep audit logs. Carries the failure code,
# message, last-known stage / step, and the per-step status table
# at the moment of failure.
ARTIFACT_KIND_ERROR_REPORT = "error_report"
# Pre-finalize validation snapshot. Persisted by the workflow's
# COMPLETED transition (or by the FAILED handler when validation
# itself triggered the failure). Carries the list of validation
# errors (empty when validation passed) plus the rules that ran,
# so operators can see WHY the run was marked succeeded /
# completed_with_warnings / failed without re-running validation.
ARTIFACT_KIND_VALIDATION_REPORT = "validation_report"
# Final-summary artifact written at terminal state — succeeded OR
# failed. Carries the at-a-glance run outcome: final_status,
# document_id, planner mode, executed-stage tally, artifact tally
# by kind, warning_count, duration. Backs the "what happened in
# this run?" overview without forcing the FE to assemble it from
# separate endpoints.
ARTIFACT_KIND_FINAL_SUMMARY = "final_summary"
# Per-stage validation report. One artifact per durable stage per
# run (compile / generate_chunks / enrich / graph). Carries the
# `StageValidationResult` payload so operators can audit which
# checks ran and which tripped. Distinct from the run-level
# `validation_report` (which aggregates the per-stage outcomes
# at terminal transition). See
# [`docs/ingestion-stage-validation.md`](../../docs/ingestion-stage-validation.md).
ARTIFACT_KIND_STAGE_VALIDATION_REPORT = "stage_validation_report"
# Compile-strategy + safety-retry summary. One artifact per
# compile-stage execution (or sequence of retries) carrying the
# AssessmentPlan, the resolved CompileConfig, the per-attempt
# audit list, and the final-quality verdict. The FE's run-detail
# Compile Strategy tab reads this to render the timeline.
ARTIFACT_KIND_COMPILE_STRATEGY_REPORT = "compile_strategy_report"
# Post-compile enrich-plan assessment. Built by the rule-based
# `j1.processing.enrich_assessment` assessor immediately after
# compile success; carries the recommendation (skip/optional/
# recommended/required), per-task decisions, source signals, and
# decision_source. Read by the FE's enrich-plan card and by future
# stage-gate logic that wants to consult the recommendation.
ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN = "post_compile_enrich_plan"
# Pre-compile initial execution plan. Built by
# `j1.processing.initial_execution_plan.build_initial_execution_plan`
# immediately after the cheap document profile finishes; carries the
# selected domain_profile_id, enrichment_policy, candidate enrichment
# modules, cheap_signals snapshot, and the wrapped compile-stage
# `AssessmentPlan`. Read by the FE's initial-plan panel and by the
# post-compile assessor to derive the candidate list.
ARTIFACT_KIND_INITIAL_EXECUTION_PLAN = "initial_execution_plan"

__all__ = [
    "ARTIFACT_KIND_CHUNK",
    "ARTIFACT_KIND_COMPILED_TEXT",
    "ARTIFACT_KIND_ERROR_REPORT",
    "ARTIFACT_KIND_FINAL_SUMMARY",
    "ARTIFACT_KIND_INITIAL_EXECUTION_PLAN",
    "ARTIFACT_KIND_PARSED_CONTENT_MANIFEST",
    "ARTIFACT_KIND_PARSED_SOURCE",
    "ARTIFACT_KIND_PLANNING_RESULT",
    "ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN",
    "ARTIFACT_KIND_COMPILE_STRATEGY_REPORT",
    "ARTIFACT_KIND_STAGE_VALIDATION_REPORT",
    "ARTIFACT_KIND_VALIDATION_REPORT",
    "ArtifactDraft",
    "ArtifactProcessingResult",
    "CostBreakdown",
    "CostResult",
    "ModelResponse",
    "ProcessingResult",
    "QueryResult",
    "ResultStatus",
    "ReviewItemResult",
]


@dataclass(frozen=True)
class ArtifactDraft:
    kind: str
    content: bytes
    suggested_extension: str = ""
    source_document_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    review_required: bool = False


@dataclass(frozen=True)
class ProcessingResult:
    status: ResultStatus
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactProcessingResult:
    status: ResultStatus
    drafts: list[ArtifactDraft] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    cost_events: list[CostBreakdown] = field(default_factory=list)
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryResult:
    status: ResultStatus
    answer: str | None = None
    citations: list[str] = field(default_factory=list)
    cost_events: list[CostBreakdown] = field(default_factory=list)
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewItemResult:
    status: ResultStatus
    review_item_id: str
    review_status: ReviewStatus
    actor: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    cost: CostBreakdown
    metadata: dict[str, Any] = field(default_factory=dict)
