"""User-selectable execution profiles for ingestion.

An `ExecutionProfile` is the user-facing answer to "how expensive
should this ingest be?" — distinct from:

 * `CompileMode` ([assessment.py](./assessment.py)) — how *intensely*
   the parser runs (standard vs deep) for ONE document. Set by the
   rule-based planner from document signals.
 * `PostCompileEnrichPlan` ([enrich_assessment.py](./enrich_assessment.py))
   — whether enrichment-stage LLM tasks (vision captioning,
   requirement/risk extraction, etc.) should run AFTER compile,
   based on what compile actually produced.

The `ExecutionProfile` sits ABOVE both: it is the user's explicit
choice between "make it queryable cheaply" and "give me the best
quality available." Once selected it is the source of truth and
overrides the planner's recommendation and the enrich plan when
they would enable work the profile forbids.

Three profiles are defined:

 * `minimum_queryable` — the honest minimum. MinerU is skipped
   when possible (text fast paths), LightRAG's built-in entity
   extraction is short-circuited via a no-op `llm_model_func`
   injected at adapter construction, no multimodal enrichment,
   no graph build, no domain enrichment, no validation. The
   document is queryable via vector retrieval.
 * `standard` — pragmatic default. Compile runs through its
   normal path including LightRAG's built-in entity extraction
   (which the upstream library does NOT let us disable). Enrich,
   graph build, and domain enrichment are skipped unless the
   caller explicitly opts in. Honest about what it costs: every
   LLM call is logged with `selected_profile=standard`.
 * `advanced` — full quality. Enrichment is gated by the
   `PostCompileEnrichPlan`, graph build runs when the caller
   supplies a `graph_builder_kind`, domain enrichment and
   validation are enabled.

The recommendation is produced by `recommend_profile_from_assessment`
based on the same signals the planner uses for compile mode.
The user is shown the recommendation alongside `available_profiles`
and chooses; the chosen profile is persisted on the run and is
the authoritative gate for every downstream "should this stage
run?" check.

Naming hygiene:

 * Profile values are wire strings — dashboards filter on them,
   audit logs key off them. Do not rename without a migration.
 * Profiles describe USER INTENT. Adapter-level toggles
   (e.g. `enable_image_processing`) live in the adapter and are
   derived from the profile, not the other way around.
 * Hard env safety knobs (`J1_ALLOW_ADVANCED_INGEST=false`) can
   refuse a profile but they cannot silently swap one for
   another — refusal is explicit and surfaces to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = "1"


class ExecutionProfile(StrEnum):
    """User-selectable ingestion profile.

    Wire strings are stable — dashboards key off them, audit logs
    quote them verbatim, and `IngestionRun.metadata.selected_execution_profile`
    persists this value as-is. Don't rename without a migration.
    """

    MINIMUM_QUERYABLE = "minimum_queryable"
    STANDARD = "standard"
    ADVANCED = "advanced"


# Operator-facing labels for the profile catalogue. The FE renders
# these next to each radio button in the profile picker. Kept here
# (not in the FE) so backend and frontend stay in sync via one
# source of truth.
PROFILE_LABELS: dict[ExecutionProfile, str] = {
    ExecutionProfile.MINIMUM_QUERYABLE: "Minimum Queryable",
    ExecutionProfile.STANDARD: "Standard",
    ExecutionProfile.ADVANCED: "Advanced",
}


# `profile_selected_by` vocabulary persisted on the run. Stable
# strings — UI labels key off them and audit dashboards filter on
# them. "user" means a human chose explicitly via the UI;
# "recommendation" means the user accepted the planner's
# recommendation unchanged; "system" means no UI selection
# happened and the backend default kicked in.
SELECTED_BY_USER = "user"
SELECTED_BY_RECOMMENDATION = "recommendation"
SELECTED_BY_SYSTEM_DEFAULT = "system"
SELECTED_BY_ENV_OVERRIDE = "env_override"


# `profile_selection_source` vocabulary persisted on the run.
# "ui" / "rest" / "env" / "default". The UI value tells us a human
# clicked through the two-step flow; rest means a programmatic
# caller passed `selected_profile` on the index request; env means
# `J1_DEFAULT_INGEST_PROFILE` set it; default means none of the
# above and the safe-default kicked in.
SELECTION_SOURCE_UI = "ui"
SELECTION_SOURCE_REST = "rest"
SELECTION_SOURCE_ENV = "env"
SELECTION_SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class ProfileCapabilities:
    """The boolean matrix expressing what a profile allows.

    Every downstream "should this run?" check should read these
    flags rather than branching on the profile value directly.
    That keeps the profile semantics in one place — if a new
    capability ships later, every check picks it up by reading
    the new flag, not by adding a new conditional everywhere.

    Flags follow the naming convention `<stage>_<capability>`:
    `compile_*` flags travel into the adapter, `enrich_*` /
    `graph_*` / `index_*` flags gate the corresponding workflow
    stages, `multimodal_*` and `domain_*` flags gate enrichment
    sub-tasks.
    """

    # Compile-stage controls. Travel into the adapter via
    # `CompileConfig.to_config_overrides()` plus the new
    # `disable_entity_extraction` flag.
    compile_use_text_fast_path: bool
    compile_multimodal_processing: bool  # images / tables / equations
    compile_lightrag_entity_extraction: bool  # LightRAG's built-in stage-2
    compile_lightrag_relationship_extraction: bool  # same pipeline as entity

    # Stage-level gates checked in `_stage_enabled`.
    run_enrich: bool
    run_graph_build: bool
    run_index: bool

    # Enrichment sub-task gates checked by the enrichment runner.
    enrich_image_captioning: bool
    enrich_vision_enrichment: bool
    enrich_table_enrichment: bool
    enrich_requirement_extraction: bool
    enrich_risk_extraction: bool
    enrich_quality_assessment: bool

    # Domain-aware gates.
    domain_enrichment: bool
    validation_tasks: bool


# Per-profile capability matrix. Source of truth for profile
# semantics — every downstream gate reads from this rather than
# branching on the profile value. Keep this dictionary the only
# place that maps a profile to its capability set.
PROFILE_CAPABILITIES: dict[ExecutionProfile, ProfileCapabilities] = {
    ExecutionProfile.MINIMUM_QUERYABLE: ProfileCapabilities(
        compile_use_text_fast_path=True,
        compile_multimodal_processing=False,
        compile_lightrag_entity_extraction=False,
        compile_lightrag_relationship_extraction=False,
        run_enrich=False,
        run_graph_build=False,
        run_index=True,  # required for query
        enrich_image_captioning=False,
        enrich_vision_enrichment=False,
        enrich_table_enrichment=False,
        enrich_requirement_extraction=False,
        enrich_risk_extraction=False,
        enrich_quality_assessment=False,
        domain_enrichment=False,
        validation_tasks=False,
    ),
    ExecutionProfile.STANDARD: ProfileCapabilities(
        compile_use_text_fast_path=True,
        compile_multimodal_processing=True,
        # The adapter cannot honestly disable these — LightRAG's
        # `ainsert()` runs entity + relationship extraction
        # unconditionally. We leave these True for `standard` so
        # the workflow doesn't pretend otherwise; the adapter
        # surfaces the limitation via `unsupported_profile_controls`.
        compile_lightrag_entity_extraction=True,
        compile_lightrag_relationship_extraction=True,
        run_enrich=False,
        run_graph_build=False,
        run_index=True,
        enrich_image_captioning=False,
        enrich_vision_enrichment=False,
        enrich_table_enrichment=False,
        enrich_requirement_extraction=False,
        enrich_risk_extraction=False,
        enrich_quality_assessment=False,
        domain_enrichment=False,
        validation_tasks=False,
    ),
    ExecutionProfile.ADVANCED: ProfileCapabilities(
        compile_use_text_fast_path=True,
        compile_multimodal_processing=True,
        compile_lightrag_entity_extraction=True,
        compile_lightrag_relationship_extraction=True,
        run_enrich=True,
        run_graph_build=True,
        run_index=True,
        enrich_image_captioning=True,
        enrich_vision_enrichment=True,
        enrich_table_enrichment=True,
        enrich_requirement_extraction=True,
        enrich_risk_extraction=True,
        enrich_quality_assessment=True,
        domain_enrichment=True,
        validation_tasks=True,
    ),
}


def capabilities_for(profile: ExecutionProfile) -> ProfileCapabilities:
    """Lookup helper that raises a clear error for unknown profiles.

    Used everywhere a stage gate needs to consult the capability
    matrix. Raising rather than defaulting prevents a typo in a
    new profile silently degrading to a permissive set.
    """
    try:
        return PROFILE_CAPABILITIES[profile]
    except KeyError as exc:  # pragma: no cover — guarded by StrEnum
        raise ValueError(f"unknown ExecutionProfile: {profile!r}") from exc


# Operator-facing cost descriptors surfaced in the assessment-plan
# API response and rendered on the profile picker. Stable strings;
# the FE may key off them for icon selection.
_SPEED = {
    ExecutionProfile.MINIMUM_QUERYABLE: "fast",
    ExecutionProfile.STANDARD: "medium",
    ExecutionProfile.ADVANCED: "slow",
}
_LLM_USAGE = {
    # Entity extraction is short-circuited, no vision, no enrich.
    ExecutionProfile.MINIMUM_QUERYABLE: "none_or_minimal",
    # LightRAG entity extraction still fires inside compile but no
    # enrich/graph/multimodal LLM calls are made downstream.
    ExecutionProfile.STANDARD: "limited",
    # Full pipeline: entity extraction + vision + enrich + graph.
    ExecutionProfile.ADVANCED: "high",
}


def profile_details(profile: ExecutionProfile) -> dict[str, Any]:
    """Render the operator-facing description of a profile.

    Returned to the FE as part of the `available_profiles` payload
    on the assessment-plan response. Keep field names stable —
    the FE keys off them directly.
    """
    caps = capabilities_for(profile)
    return {
        "id": profile.value,
        "label": PROFILE_LABELS[profile],
        "queryable": caps.run_index,
        "expected_speed": _SPEED[profile],
        "expected_llm_usage": _LLM_USAGE[profile],
        "graph_enabled": caps.run_graph_build,
        "multimodal_processing": caps.compile_multimodal_processing,
        "enrichment_enabled": caps.run_enrich,
        "domain_enrichment_enabled": caps.domain_enrichment,
        "validation_enabled": caps.validation_tasks,
        # Honest disclosure: the entity-extraction tax is unavoidable
        # for `standard` / `advanced`; the operator should see it on
        # the card so they can pick `minimum_queryable` when the
        # cost matters.
        "compile_lightrag_extraction": (
            caps.compile_lightrag_entity_extraction
            or caps.compile_lightrag_relationship_extraction
        ),
    }


# Safe backend default when no UI selection arrives and no env
# override is set. `STANDARD` (not `MINIMUM_QUERYABLE`) because
# `minimum_queryable` skips graph and enrich entirely — a silent
# default to it would surprise existing callers who relied on
# the old `standard` mode producing entities. The investigation
# report documents the trade-off; flip to `MINIMUM_QUERYABLE` once
# the FE is wired and operators expect to choose explicitly.
DEFAULT_PROFILE: ExecutionProfile = ExecutionProfile.STANDARD


def recommend_profile_from_assessment(
    *,
    has_images: bool,
    has_tables: bool,
    has_scanned_pages: bool,
    text_extractable_ratio: float | None,
    page_count: int | None,
) -> tuple[ExecutionProfile, tuple[str, ...]]:
    """Map deterministic pre-compile signals to a recommended profile.

    Returns `(recommended_profile, reasons)`. The reasons tuple is
    surfaced verbatim on the FE profile picker so the operator
    can see WHY the recommendation was made.

    Rules — kept simple on purpose:

    * Scanned PDFs or low text extractability → `advanced` (needs
      OCR + vision + likely table/figure enrichment to be useful).
    * Images or tables present → `advanced` (the user almost
      certainly wants captions / table summaries / cross-references).
    * Plain text with no multimodal signal → `standard` (no
      enrichment value-add, but keep entity extraction so graph
      queries work if the user later asks).
    * Very large documents (page_count > 100) → `standard` (full
      `advanced` pipeline on a long doc is hours; the user should
      opt in explicitly).

    `minimum_queryable` is NEVER recommended automatically —
    debugging / latency-investigation is a deliberate operator
    choice, not a profile the planner should pick.
    """
    reasons: list[str] = []

    if has_scanned_pages:
        reasons.append("Document contains scanned pages; OCR + vision enrichment is expected to help.")
        return ExecutionProfile.ADVANCED, tuple(reasons)

    if text_extractable_ratio is not None and text_extractable_ratio < 0.1:
        reasons.append(
            "Very little text could be extracted directly; OCR-based parsing and vision enrichment recommended."
        )
        return ExecutionProfile.ADVANCED, tuple(reasons)

    multimodal = []
    if has_images:
        multimodal.append("images")
    if has_tables:
        multimodal.append("tables")
    if multimodal:
        reasons.append(
            f"Document contains {' and '.join(multimodal)}; enrichment is recommended for better queries."
        )
        if page_count is not None and page_count > 100:
            reasons.append(
                "Document is long (>100 pages); confirm advanced explicitly — full enrichment may be slow."
            )
            return ExecutionProfile.STANDARD, tuple(reasons)
        return ExecutionProfile.ADVANCED, tuple(reasons)

    reasons.append("Document is text-only; standard mode is the balanced choice.")
    if page_count is not None and page_count > 100:
        reasons.append("Long document — standard avoids the cost of full enrichment.")
    return ExecutionProfile.STANDARD, tuple(reasons)


@dataclass(frozen=True)
class UnsupportedProfileControl:
    """One profile-driven capability control the adapter could not
    honor. Persisted on the run metadata so the FE can disclose
    "you asked for `minimum_queryable` but the installed RAGAnything
    couldn't disable image processing" without users having to read
    server logs to know their profile is partially a fiction.

    Field shape matches the task's wire spec:
      `control` — the canonical knob name (matches a
        `ProfileCapabilities` field).
      `requested_value` — what the profile asked for (typically
        `false` for a "disable X" request).
      `reason` — adapter-side explanation of why it couldn't be
        honored. Operator-readable.
      `impact` — short sentence describing the user-visible
        consequence. Drives the FE warning banner copy.
    """

    control: str
    requested_value: bool
    reason: str
    impact: str

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the wire shape persisted on
        `IngestionRun.metadata.unsupported_profile_controls`."""
        return {
            "control": self.control,
            "requested_value": self.requested_value,
            "reason": self.reason,
            "impact": self.impact,
        }


# Capability label → bridge-side `unhandled_capabilities` token.
# When the adapter reports a capability under one of these tokens,
# the corresponding profile control is unsupported. Kept here
# (not in the adapter) because the profile system is the consumer
# — the adapter is just a producer of tokens.
_CAPABILITY_TO_UNHANDLED_TOKEN: dict[str, str] = {
    "compile_multimodal_processing": "image_extraction",
    # Future: `compile_table_processing` and `compile_equation_processing`
    # if/when the matrix splits multimodal into per-modality flags.
    # The bridge already separates the underlying config overrides
    # (`enable_image_processing` / `enable_table_processing` /
    # `enable_equation_processing`), so the split is one matrix
    # edit away.
}


def detect_unsupported_controls(
    *,
    profile: ExecutionProfile,
    unhandled_capabilities: tuple[str, ...] | list[str] = (),
) -> tuple[UnsupportedProfileControl, ...]:
    """Translate adapter-side `unhandled_capabilities` into the
    structured profile-control warning list.

    The adapter's bridge reports `unhandled_capabilities` whenever
    the installed RAGAnything / RAGAnythingConfig doesn't expose
    the config field that would have enforced a capability flag
    (see [`_bridge.py`](../providers/raganything/_bridge.py)
    `dropped_overrides` plumbing). This function maps those
    vendor-version-drift signals back to profile capability names
    so the operator sees them in profile vocabulary, not vendor
    vocabulary.

    Only emits warnings for controls the profile actually CARES
    about (e.g. `compile_multimodal_processing=False` on
    `minimum_queryable`). When the profile permits multimodal
    processing (e.g. `advanced`), an unhandled `image_extraction`
    capability is informational, not a profile violation — those
    are reported on the existing `unhandled_capabilities` field
    and don't appear here.

    Entity / relationship extraction are NOT in the unhandled
    list when the J1 no-op `llm_model_func` hook is wired — we
    own that callable, so the control is always honored. If a
    future deployment swaps in a custom compiler that DOESN'T
    honor `disable_entity_extraction`, the workflow can detect
    that separately (e.g. by counting LightRAG entity-extraction
    LLM calls > 0 with a minimum_queryable run) and call this
    function with the appropriate token added.
    """
    caps = capabilities_for(profile)
    unhandled_set = {str(t) for t in unhandled_capabilities}
    results: list[UnsupportedProfileControl] = []

    if (
        not caps.compile_multimodal_processing
        and _CAPABILITY_TO_UNHANDLED_TOKEN["compile_multimodal_processing"]
        in unhandled_set
    ):
        results.append(
            UnsupportedProfileControl(
                control="disable_multimodal_processing",
                requested_value=True,
                reason=(
                    "RAGAnythingConfig does not expose the per-modality "
                    "processing toggles (`enable_image_processing` / "
                    "`enable_table_processing` / `enable_equation_processing`) "
                    "in the installed vendor version."
                ),
                impact=(
                    f"{profile.value} requested multimodal processing to be "
                    "disabled, but the adapter cannot enforce this. The "
                    "compile stage may still run image/table extraction."
                ),
            ),
        )

    return tuple(results)


@dataclass(frozen=True)
class ExecutionProfileSelection:
    """Audit record of how the final profile was chosen.

    Persisted on `IngestionRun.metadata` so a later debug pass can
    answer "who picked this profile and what was the alternative
    the planner suggested?" without replaying the workflow.

    `recommended_profile` is what the planner suggested.
    `selected_profile` is what the run actually executed.
    `selected_by` / `selection_source` use the vocabulary
    constants defined above.
    `reasons` are the planner's reasons, persisted so the audit
    record stands alone.
    `warnings` are operator-readable strings (e.g. "profile
    advanced requested but J1_ALLOW_ADVANCED_INGEST=false; run
    refused" — recorded by the REST handler when applicable).
    """

    recommended_profile: ExecutionProfile
    selected_profile: ExecutionProfile
    selected_by: str
    selection_source: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the dict shape persisted on
        `IngestionRun.metadata` and surfaced on REST responses.
        Schema is stable — dashboards key off field names."""
        return {
            "schema_version": SCHEMA_VERSION,
            "assessment_recommended_profile": self.recommended_profile.value,
            "selected_execution_profile": self.selected_profile.value,
            "profile_selected_by": self.selected_by,
            "profile_selection_source": self.selection_source,
            "profile_reasons": list(self.reasons),
            "profile_warnings": list(self.warnings),
        }
