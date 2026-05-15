"""Public DTOs returned by `IngestionResultReviewService`.


Naming policy: snake_case Python fields, serialized as camelCase via
a local `CamelModel` base. We intentionally don't import the
adapter-layer `CamelModel` (`j1.adapters.rest.envelope`) — core
modules can't depend on the adapters layer (enforced by
`tests/test_integration_layer.py`). The two definitions agree on
config; if the convention changes, both move together.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Local copy of the standard envelope's `CamelModel`.

 Lives here because `j1.ingestion_review` is a core module and is
 forbidden from importing `j1.adapters`. Same shape so the wire
 format is identical to every other endpoint."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


# ---- Step / warning records --------------------------------------


class StepErrorDTO(CamelModel):
    """Compact error attached to a failed step. Mirrors `StepError`."""

    type: str
    message: str
    retryable: bool = False


class StepResultDTO(CamelModel):
    """Per-stage outcome. Mirrors `StepResult` with workflow-only
 fields surfaced so the UI can answer 'why did this stage run / not
 run / fail?' without consulting raw audit events."""

    step: str
    status: str
    required: bool
    source: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    reason: str | None = None
    error: StepErrorDTO | None = None
    artifact_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class WarningDTO(CamelModel):
    """One warning surfaced from the run.

 `severity` is one of `"info"`, `"warning"`, `"error"` (lower-cased
 relative to the audit-log severity strings — keeps the UI palette
 keys stable).

 Source-traceability fields are populated whenever the originating
 progress event / step result carried them. Projection MUST NOT
 drop these fields when they are available."""

    code: str
    message: str
    severity: str = "warning"
    step: str | None = None
    document_id: str | None = None
    page: int | None = None
    chunk_id: str | None = None
    artifact_id: str | None = None


# ---- Availability ------------------------------------------------


class AvailabilityDTO(CamelModel):
    """Per-view availability flag plus an optional reason.

 `reason` is populated only when `available=False`. The reason
 strings are authored in `availability.py` so copy stays consistent
 across tabs."""

    available: bool
    reason: str | None = None


class AvailableViewsDTO(CamelModel):
    """Which Result tabs the FE should enable for this run."""

    chunks: AvailabilityDTO
    assets: AvailabilityDTO
    graph: AvailabilityDTO
    quality: AvailabilityDTO
    raw_artifacts: AvailabilityDTO
    # Validation tab is enabled for terminal-success runs that
    # produced at least one chunk artifact (otherwise there's nothing
    # to query). Manual test query is the entry point.
    validation: AvailabilityDTO
    # Content Inventory tab — visible as soon as the compile activity
    # has emitted a `parsed_content_manifest` artifact, even while
    # downstream stages (enrich / graph / index) are still running.
    # Lets reviewers inspect what the parser actually found while the
    # rest of the pipeline finishes. Optional with default for
    # backward compatibility — runs that pre-date the manifest
    # artifact carry the legacy availability set without crashing
    # older FE bundles.
    parsed_content: AvailabilityDTO = AvailabilityDTO(
        available=False,
        reason="No parsed-content manifest is available for this run.",
    )
    # Planning Report tab — visible as soon as the planner has emitted
    # a `plan.generated` audit entry. Surfaces the planner's mode,
    # policy, per-step decisions, and (when LLM-assisted planning is
    # enabled) the LLM recommendation. Optional default keeps older
    # FE bundles + legacy runs forward-compatible.
    planning: AvailabilityDTO = AvailabilityDTO(
        available=False,
        reason="No planning report is available for this run.",
    )


# ---- Quality summary --------------------------------------------


class QualitySummaryDTO(CamelModel):
    """Compact quality projection embedded in the run summary.

 The full quality report lives behind `/quality-report`;
 this is just enough for the Overview tab's scorecard."""

    overall_confidence: float | None = None
    warning_count: int = 0
    low_confidence_count: int = 0


# ---- Run summary -------------------------------------------------


class ArtifactRecordDTO(CamelModel):
    """One artifact, projected to a UI-safe shape.

 Mirrors `j1.artifacts.models.ArtifactRecord` minus the runtime-
 only fields (`project`, raw enum types) — the FE never reads
 those, and we'd rather not re-camelize them per request."""

    artifact_id: str
    kind: str
    location: str
    content_hash: str
    byte_size: int
    status: str
    review_status: str
    version: int
    created_at: str
    updated_at: str
    source_document_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinkedAssetDTO(CamelModel):
    """Reference from a chunk to a downstream asset (image, table, etc.).

 Producers may emit this so the FE can show "this chunk has a
 table on page 5" without a separate lookup. `artifact_id` lets
 the FE link straight to the artifact-content endpoint."""

    artifact_id: str
    kind: str | None = None


class ChunkPreviewDTO(CamelModel):
    """One chunk in list view — preview only, no full body.

 Returned by `GET /ingestion-runs/{run_id}/chunks`. The `preview`
 is a short excerpt (≤240 chars) suitable for a list row; the FE
 fetches the detail endpoint when the user opens the drawer."""

    chunk_id: str
    preview: str
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    title: str | None = None
    token_count: int | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    linked_assets: list[LinkedAssetDTO] = Field(default_factory=list)
    source_artifact_id: str | None = None


class ChunkDetailDTO(CamelModel):
    """One chunk in detail view — full body + lineage.

 Returned by `GET /ingestion-runs/{run_id}/chunks/{chunk_id}`."""

    chunk_id: str
    body: str
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    title: str | None = None
    token_count: int | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    linked_assets: list[LinkedAssetDTO] = Field(default_factory=list)
    source_artifact_id: str | None = None
    lineage: dict[str, Any] = Field(default_factory=dict)


class ChunkPageDTO(CamelModel):
    """Paginated chunk list returned by
 `GET /ingestion-runs/{run_id}/chunks`."""

    items: list[ChunkPreviewDTO] = Field(default_factory=list)
    page: int
    page_size: int
    total: int


class ModalityConfidenceDTO(CamelModel):
    """Per-modality confidence breakdown surfaced inside the quality
 report. `modality` is a free-form producer-supplied label (e.g.
 "tables", "images", "ocr") — neutral for the FE to render."""

    modality: str
    confidence: float
    sample_count: int | None = None


class SkippedStepDTO(CamelModel):
    """One stage that the workflow / planner / policy decided not to
 run. `policy` (when set) names the decision driver — `policy`,
 `planner`, `caller`, `default`, `config`."""

    step: str
    reason: str | None = None
    policy: str | None = None


class FailedOptionalStepDTO(CamelModel):
    """One optional stage that was attempted and failed. The run
 didn't downgrade to FAILED because the stage was non-required,
 but the FE surfaces these so reviewers see what happened."""

    step: str
    reason: str | None = None
    error_type: str | None = None


class LowConfidenceFindingDTO(CamelModel):
    """One specific low-confidence region/finding. Source-traceability
 fields are populated whenever the underlying finding carried them
 — projection MUST NOT drop these when available."""

    score: float
    category: str
    message: str | None = None
    page: int | None = None
    chunk_id: str | None = None
    artifact_id: str | None = None


class QualityReportDTO(CamelModel):
    """Neutral quality report returned by
 `GET /ingestion-runs/{run_id}/quality-report`.

 Composed from `enriched.confidence_assessment`,
 `enriched.consistency_findings`, audit-log warnings, and persisted
 step results — never exposes vendor-specific JSON shapes. The
 optional `raw_debug` field carries the unprojected source JSON
 when callers explicitly opt in via `?includeRaw=true`."""

    overall_confidence: float | None = None
    modality_confidences: list[ModalityConfidenceDTO] = Field(default_factory=list)
    warnings: list[WarningDTO] = Field(default_factory=list)
    skipped_steps: list[SkippedStepDTO] = Field(default_factory=list)
    failed_optional_steps: list[FailedOptionalStepDTO] = Field(default_factory=list)
    low_confidence_findings: list[LowConfidenceFindingDTO] = Field(default_factory=list)
    raw_debug: dict[str, Any] | None = None


class GraphEntityDTO(CamelModel):
    """One node in the graph snapshot. Producer-vendor field names
 (`__id__`, `__entity_type__`, `__source_id__`, etc.) are mapped
 to these neutral fields by the projector — never exposed."""

    id: str
    label: str
    type: str | None = None
    description: str | None = None
    source_chunk_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphRelationDTO(CamelModel):
    """One edge in the graph snapshot."""

    id: str
    source_entity_id: str
    target_entity_id: str
    label: str | None = None
    type: str | None = None
    description: str | None = None
    weight: float | None = None
    source_chunk_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphStatsDTO(CamelModel):
    """Aggregate counts for the graph snapshot. `entityCount` and
 `relationCount` are the FULL counts before truncation — the FE
 can compare against `truncated.limits` to know if a re-fetch
 with a higher cap is worthwhile."""

    entity_count: int = 0
    relation_count: int = 0
    source_artifact_ids: list[str] = Field(default_factory=list)


class GraphTruncationLimitsDTO(CamelModel):
    """Caps the projector applied. Mirrors the query parameters."""

    max_nodes: int
    max_edges: int


class GraphTruncatedDTO(CamelModel):
    """Per-list truncation flags. The FE shows the table fallback +
 "graph too large" banner when either flag is set."""

    entities: bool = False
    relations: bool = False
    limits: GraphTruncationLimitsDTO


class GraphUnavailableDTO(CamelModel):
    """Why the graph view isn't available. Single source of truth
 for the reason string — matches the `availableViews.graph.reason`
 shown in the run summary."""

    reason: str


class GraphSnapshotDTO(CamelModel):
    """Neutral graph snapshot returned by
 `GET /ingestion-runs/{run_id}/graph`.

 When the run produced no graph artifacts (skipped by policy /
 planner / failed), the projector returns a DTO with empty
 entities/relations and `unavailable` populated — the FE renders
 the empty state with the reason."""

    stats: GraphStatsDTO
    entities: list[GraphEntityDTO] = Field(default_factory=list)
    relations: list[GraphRelationDTO] = Field(default_factory=list)
    truncated: GraphTruncatedDTO
    unavailable: GraphUnavailableDTO | None = None


class ArtifactPageDTO(CamelModel):
    """Paginated artifact list returned by
 `GET /ingestion-runs/{run_id}/artifacts`.

 `total` reflects the filtered set BEFORE pagination — same
 convention the existing `IngestionRunListRecord` uses."""

    items: list[ArtifactRecordDTO] = Field(default_factory=list)
    page: int
    page_size: int
    total: int


class RunSummaryDTO(CamelModel):
    """Top-level review summary for one run.

 Returned by `GET /ingestion-runs/{run_id}/summary`. Drives the
 Results > Overview tab and gates the other tabs via
 `available_views`."""

    run_id: str
    status: str
    duration_ms: int | None = None
    document_ids: list[str] = Field(default_factory=list)
    steps: list[StepResultDTO] = Field(default_factory=list)
    artifact_counts: dict[str, int] = Field(default_factory=dict)
    total_bytes: int = 0
    warnings: list[WarningDTO] = Field(default_factory=list)
    quality_summary: QualitySummaryDTO | None = None
    available_views: AvailableViewsDTO


# ---- Content Inventory (parsed-content manifest projection) -----


class ContentInventorySourceDTO(CamelModel):
    """Provenance for the parsed content — which compiler / parser
 produced the manifest. The FE shows this in the Content
 Inventory tab's metadata strip."""

    compiler: str | None = None
    parser: str | None = None
    parser_version: str | None = None
    parse_method: str | None = None
    profile: str | None = None


class ContentInventorySummaryDTO(CamelModel):
    """Aggregate counts the parser surfaced. Each field is optional
 so older runs without a particular signal don't crash the FE.

 Mirrors the shape of `ParsedContentStats` in
 `j1.processing.manifest`, but renamed for FE clarity (and
 camelCase on the wire)."""

    page_count: int | None = None
    text_block_count: int = 0
    table_count: int = 0
    image_count: int = 0
    formula_count: int = 0
    heading_count: int | None = None
    other_count: int = 0
    total_items: int = 0


class ContentInventoryItemDTO(CamelModel):
    """One per-element entry. Producers (the compile bridge) may
 populate `items[]` selectively — typically only the first N
 images and tables for triage UI. Items present here are NOT a
 contract that all parser output is enumerable; the
 `summary` counts are the authoritative aggregates."""

    item_id: str
    type: str  # "text" | "table" | "image" | "formula" | "heading" | "other"
    page: int | None = None
    location: str | None = None
    preview: str | None = None
    confidence: float | None = None
    passed_to_enrichment: bool | None = None
    skipped: bool = False
    skip_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentInventoryDTO(CamelModel):
    """Normalized view of the run's `parsed_content_manifest`
 artifact. The FE's Content Inventory tab consumes this directly.

 `status` semantics:
 * `"completed"` — manifest exists, parser produced something.
 * `"empty"` — manifest exists but every count is 0 (parser
 ran but found nothing extractable; shouldn't happen with
 non-trivial input).
 * `"unavailable"` — no manifest artifact for this run. The
 FE renders the empty state with the corresponding reason.
 """

    run_id: str
    document_id: str | None = None
    document_name: str | None = None
    status: str
    source: ContentInventorySourceDTO = Field(default_factory=ContentInventorySourceDTO)
    summary: ContentInventorySummaryDTO = Field(default_factory=ContentInventorySummaryDTO)
    items: list[ContentInventoryItemDTO] = Field(default_factory=list)
    raw_artifact_id: str | None = None
    unavailable_reason: str | None = None


# ---- Planning Report (richer projection over IngestPlan) ----------


class PlanningStepDecisionDTO(CamelModel):
    """One per-stage decision in the Planning Report.

 Mirrors the workflow-gate fields of `PlannedStep` but in the
 camelCase DTO shape and with the projector-friendly names the FE
 Planning Report tab expects."""

    step_id: str
    stage: str
    decision: str  # "RUN" | "SKIP" | "CONDITIONAL"
    enabled: bool
    required: bool
    source: str
    reason: str | None = None
    risk_level: str = "low"
    estimated_cost_tier: str = "NONE"
    llm_class: str = "none"
    expected_engine: str | None = None
    expected_provider: str | None = None
    dependency_step_ids: list[str] = Field(default_factory=list)
    warning: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanningContentDigestDTO(CamelModel):
    """Lightweight digest of the parsed-content manifest, used both as
 the "evidence" the rule-based assessment cites AND, when LLM-
 assisted planning is enabled, as the bounded sample fed to the
 planner LLM.

 Privacy: the digest is sampled — it never includes the full raw
 document. The two cap fields (`sampled_block_count`,
 `max_preview_chars`) record the boundary the projector enforced
 so reviewers can audit what was sent."""

    page_count: int | None = None
    text_block_count: int = 0
    table_count: int = 0
    image_count: int = 0
    formula_count: int = 0
    heading_count: int | None = None
    total_items: int = 0
    sampled_block_count: int = 0
    max_preview_chars: int = 0


class PlanningAssessmentDTO(CamelModel):
    """The rule-based assessment that backed the plan.

 `mode` and `policy` come from the planner; `confidence` is the
 planner's own confidence in the decision; `reasons` is a short
 list of operator-readable strings explaining the decision (one
 per major signal — extension, scanned-pages, table-extension,
 high-risk content, …)."""

    mode: str
    policy: str
    confidence: float
    estimated_cost_level: str
    fast_llm_used: bool = False
    requires_vision: bool = False
    requires_premium_llm: bool = False
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PlanningLLMRecommendationDTO(CamelModel):
    """Optional LLM-assisted planning recommendation.

 Populated only when `J1_LLM_PLANNING_ENABLED=true` AND the LLM
 pass actually ran. The FE renders this beside the rule-based
 assessment so reviewers can compare.

 `status` semantics:
 * `"applied"` — LLM ran and the planner accepted its hint.
 * `"advisory"` — LLM ran but the planner kept its own decision
 (rule-based wins on disagreement; the LLM hint is shown for
 transparency).
 * `"failed"` — LLM call failed and `fail_open=true` kept the
 rule-based decision in place.
 * `"disabled"` — feature flag is off (default)."""

    status: str  # "disabled" | "applied" | "advisory" | "failed"
    model_profile: str | None = None
    summary: str | None = None
    failure_reason: str | None = None


class PlanningResultDTO(CamelModel):
    """Top-level Planning Report payload.

 Returned by `GET /ingestion-runs/{run_id}/planning`. Composed from
 the `planning_result` artifact (preferred) or the
 `plan.generated` audit entry as a fallback.

 `status` semantics:
 * `"completed"` — a plan was generated and the report is
 populated.
 * `"unavailable"` — no plan exists for this run (planner
 disabled, run hasn't reached the assessment stage yet, or
 this is a legacy run).

 `source` reflects how the plan was produced:
 * `"rule_based"` — deterministic post-compile assessment.
 * `"llm"` — LLM-assisted plan accepted.
 * `"rule_based_fallback"` — LLM ran but failed validation;
 rule-based output was kept.
 * `"audit_log"` — projection from the legacy `plan.generated`
 event (no post-compile artifact for this run).
 """

    run_id: str
    document_id: str | None = None
    document_name: str | None = None
    status: str
    generated_at: str | None = None
    revised: bool = False
    source: str | None = None
    planning_phase: str | None = None
    assessment: PlanningAssessmentDTO | None = None
    decisions: list[PlanningStepDecisionDTO] = Field(default_factory=list)
    digest: PlanningContentDigestDTO | None = None
    llm_recommendation: PlanningLLMRecommendationDTO = Field(
        default_factory=lambda: PlanningLLMRecommendationDTO(status="disabled"),
    )
    unavailable_reason: str | None = None
    # Post-compile fields. All optional so older bundles + audit-log-only
    # responses keep working.
    document_understanding: dict[str, Any] | None = None
    decision_summary: dict[str, Any] | None = None
    content_report: dict[str, Any] | None = None
    quality_report: dict[str, Any] | None = None
    execution_plan: dict[str, Any] | None = None
    rule_based_assessment: dict[str, Any] | None = None
    rule_based_comparison: dict[str, Any] | None = None
    next_actions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw_artifact_id: str | None = None
    # Domain pack context — selected domain, selection source,
    # confidence, evidence, applied rules, recommended-but-unsupported
    # capabilities. Always populated for post-compile artifacts;
    # `None` for legacy / audit-log fallback runs that pre-date
    # domain packs.
    domain_context: dict[str, Any] | None = None
    # Operator-facing planner mode. Mirrors the spec:
    #  `rule_based` / `llm` / `hybrid` / `rule_based_fallback`.
    # None on legacy / audit-log responses that pre-date the field.
    planner_mode: str | None = None


# ---- Clean Up Run -----------------------------------------------
#
# Phase 9 rename + refactor: "Delete Run" → "Clean Up Run". A
# cleanup hard-removes every record produced by a non-active
# ingestion run so it no longer pollutes storage, UI, query scope,
# validation, or audit. The action is *only* allowed for terminal,
# non-active, non-only-run runs that don't produce the document's
# currently active snapshot. Eligibility is checked via the same
# helper for the API, UI pre-flight, and confirmation modal so the
# rules never drift.

# Stable machine-readable reasons returned by the eligibility check
# and the rejected-cleanup response. The UI maps these to human
# copy without re-deriving the rules.
CLEANUP_REASON_OK = "OK"
CLEANUP_REASON_RUN_NOT_FOUND = "RUN_NOT_FOUND"
CLEANUP_REASON_PROCESSING_RUN = "PROCESSING_RUN"
CLEANUP_REASON_ACTIVE_RUN = "ACTIVE_RUN"
CLEANUP_REASON_ONLY_RUN = "ONLY_RUN"


class CleanUpEligibilityDTO(CamelModel):
    """Result of ``can_clean_up_run``. Single source of truth for
    the rules every caller (REST GET pre-check, POST clean-up,
    legacy DELETE wrapper, FE pre-flight) consults.

    ``reason`` is one of the ``CLEANUP_REASON_*`` codes above.
    ``blocking_references`` carries the specific ids that block
    cleanup (e.g. the active snapshot id, the sole-run id) so the
    UI can render a precise message without re-querying.
    """

    run_id: str
    allowed: bool
    reason: str
    message: str
    blocking_references: dict[str, Any] = Field(default_factory=dict)


class CleanUpDeletedCountsDTO(CamelModel):
    """Per-resource counts of things removed by one cleanup call.
    Counts are best-effort tallies — a cleanup that partially
    failed still emits the counts it succeeded on so operators can
    diagnose without re-running. The DTO is intentionally
    extensible: new resource kinds land here as additional zero-
    default fields.

    NOTE: ``chunks`` and ``enrichments`` are currently PLACEHOLDERS
    and always return 0. The cleanup *does* remove them — they're
    rolled into the ``artifacts`` count today — but the artifact
    registry doesn't expose a reliable per-kind tally on the
    delete path yet. The fields stay in the wire shape so future
    per-kind tallies land without breaking callers.
    """

    artifacts: int = 0
    # Placeholder — always 0. See class docstring.
    chunks: int = 0
    # Placeholder — always 0. See class docstring.
    enrichments: int = 0
    validation_results: int = 0
    snapshots: int = 0
    workspace_files: int = 0


class CleanUpRunResultDTO(CamelModel):
    """Result of ``POST /ingestion-runs/{run_id}/clean-up``.

    HTTP status is always 200 — the body's ``cleaned`` flag carries
    the outcome. This keeps the contract uniform whether cleanup
    ran or was refused by the eligibility check.
    """

    run_id: str
    cleaned: bool
    # ``CLEANUP_REASON_OK`` on success; refusal codes otherwise.
    reason: str = CLEANUP_REASON_OK
    message: str
    deleted_counts: CleanUpDeletedCountsDTO = Field(
        default_factory=CleanUpDeletedCountsDTO,
    )
    # Optional: the running-totals + counts the existing service
    # already returns. Kept for back-compat with operators wired to
    # the legacy DELETE response shape.
    deleted_at: str | None = None
