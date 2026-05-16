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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-checking-only import — avoids a runtime cycle through
    # `j1.domains.models` (which itself doesn't import this
    # module). The recommender accepts hints as a forward-
    # referenced annotation; the actual code path doesn't touch
    # any DomainAssessmentCapabilityHints internals beyond the
    # public attributes (`recommended`, `confidence`, `reason`).
    from j1.domains.models import (
        DomainAssessmentCapabilityHint,
        DomainAssessmentCapabilityHints,
    )


SCHEMA_VERSION = "1"


class ExecutionProfile(StrEnum):
    """User-selectable ingestion profile.

    Wire strings are stable — dashboards key off them, audit logs
    quote them verbatim, and `IngestionRun.metadata.selected_execution_profile`
    persists this value as-is. Don't rename without a migration.

    The product surfaces ONE profile: ``KNOWLEDGE_INDEX``. Every
    user-facing ingest builds the base searchable knowledge
    graph/index. Optional capability checkboxes
    (``requested_capabilities`` on the ingest request) tell the
    compiler what rich content (images / tables / equations) to
    process — they are NOT separate profiles.

    The legacy values (``minimum_queryable`` / ``standard`` /
    ``advanced``) are accepted as deprecated aliases for
    one release cycle so external callers and on-disk run records
    keep deserialising. The
    ``coerce_legacy_profile`` helper normalises every legacy value
    to ``KNOWLEDGE_INDEX``; do NOT add new behavioural branches on
    them.
    """

    KNOWLEDGE_INDEX = "knowledge_index"
    # Deprecated aliases — coerced to KNOWLEDGE_INDEX on read.
    MINIMUM_QUERYABLE = "minimum_queryable"
    STANDARD = "standard"
    ADVANCED = "advanced"


# Operator-facing labels. After the profile collapse the FE shows
# a single card ("Knowledge Index"); legacy values map to the same
# label so any dashboard rendering a historical run still resolves.
PROFILE_LABELS: dict[ExecutionProfile, str] = {
    ExecutionProfile.KNOWLEDGE_INDEX: "Knowledge Index",
    ExecutionProfile.MINIMUM_QUERYABLE: "Knowledge Index",
    ExecutionProfile.STANDARD: "Knowledge Index",
    ExecutionProfile.ADVANCED: "Knowledge Index",
}


# Profile values an external caller / FE may supply. Used by REST
# validators to accept legacy values (with a warning) but ONLY
# advertise the canonical name in the catalogue.
LEGACY_PROFILE_VALUES: frozenset[str] = frozenset({
    ExecutionProfile.MINIMUM_QUERYABLE.value,
    ExecutionProfile.STANDARD.value,
    ExecutionProfile.ADVANCED.value,
})


def coerce_legacy_profile(profile: ExecutionProfile) -> ExecutionProfile:
    """Map any legacy enum value onto ``KNOWLEDGE_INDEX``. Callers
    that receive a profile from the wire should funnel it through
    here so downstream code never sees the legacy variants. Pure /
    safe / no I/O."""
    if profile == ExecutionProfile.KNOWLEDGE_INDEX:
        return profile
    return ExecutionProfile.KNOWLEDGE_INDEX


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


# Per-profile capability matrix. ``KNOWLEDGE_INDEX`` is the
# canonical product profile and shares the previous ``STANDARD``
# matrix — every user-facing ingest builds the base searchable
# knowledge graph/index. The optional capability checkboxes (image /
# table / equation) are NOT part of this matrix — they're
# per-request flags on ``IngestRequest.requested_capabilities``.
# Legacy profile matrices are preserved verbatim so on-disk run
# records / replayed runs keep their original capability set.
PROFILE_CAPABILITIES: dict[ExecutionProfile, ProfileCapabilities] = {
    ExecutionProfile.KNOWLEDGE_INDEX: ProfileCapabilities(
        compile_use_text_fast_path=True,
        compile_multimodal_processing=True,
        # LightRAG's ainsert() runs entity + relationship extraction
        # unconditionally — the matrix is honest about it.
        compile_lightrag_entity_extraction=True,
        compile_lightrag_relationship_extraction=True,
        # The workflow's enrich/graph-build stages are gated by the
        # post-compile assessor (which consults
        # ``J1_DOMAIN_ENRICHMENT_AUTO_ENABLED``), not by the profile.
        # The capability flag stays at the profile level so the
        # workflow is WILLING to run when the post-compile assessor
        # asks for it.
        run_enrich=True,
        run_graph_build=True,
        run_index=True,
        # Enrichment sub-tasks gated by the post-compile module
        # picker.
        enrich_image_captioning=True,
        enrich_vision_enrichment=True,
        enrich_table_enrichment=True,
        enrich_requirement_extraction=True,
        enrich_risk_extraction=True,
        enrich_quality_assessment=True,
        domain_enrichment=True,
        validation_tasks=True,
    ),
    # Legacy entries — preserved for on-disk replay compat. New
    # callers MUST NOT branch on these values; the REST wire
    # boundary coerces them to KNOWLEDGE_INDEX via
    # ``coerce_legacy_profile`` before they reach planning code.
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
    ExecutionProfile.KNOWLEDGE_INDEX: "medium",
    ExecutionProfile.MINIMUM_QUERYABLE: "fast",
    ExecutionProfile.STANDARD: "medium",
    ExecutionProfile.ADVANCED: "slow",
}
_LLM_USAGE = {
    # Knowledge Index runs LightRAG entity extraction inside
    # compile; whether multimodal / enrichment LLM calls fire
    # depends on the per-request capability checkboxes + the
    # post-compile domain-enrichment env gate.
    ExecutionProfile.KNOWLEDGE_INDEX: "limited",
    # Legacy values keep their pre-collapse semantics for replay.
    ExecutionProfile.MINIMUM_QUERYABLE: "none_or_minimal",
    ExecutionProfile.STANDARD: "limited",
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
        # Honest disclosure: LightRAG's entity / relationship
        # extraction always runs inside compile — the operator
        # sees this on the card.
        "compile_lightrag_extraction": (
            caps.compile_lightrag_entity_extraction
            or caps.compile_lightrag_relationship_extraction
        ),
    }


# Default profile. ``KNOWLEDGE_INDEX`` is the only canonical
# product profile after the collapse. Legacy callers passing
# ``standard`` / ``minimum_queryable`` / ``advanced`` are coerced
# via ``coerce_legacy_profile`` at the wire boundary.
DEFAULT_PROFILE: ExecutionProfile = ExecutionProfile.KNOWLEDGE_INDEX


def recommend_profile_from_assessment(
    *,
    has_images: bool,
    has_tables: bool,
    has_scanned_pages: bool,
    text_extractable_ratio: float | None,
    page_count: int | None,
) -> tuple[ExecutionProfile, tuple[str, ...]]:
    """Map deterministic pre-compile signals to a recommended profile.

    Returns `(recommended_profile, reasons)`. Preserved for
    backwards-compat with internal callers that still ask for a
    profile name; the FE picker no longer surfaces this. New
    code should consume
    :func:`recommend_capabilities_from_assessment` instead — the
    per-checkbox recommendation that drives the post-collapse UI.

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
        reasons.append(
            "Document contains scanned pages; OCR + vision "
            "enrichment is expected to help."
        )
        return ExecutionProfile.ADVANCED, tuple(reasons)

    if text_extractable_ratio is not None and text_extractable_ratio < 0.1:
        reasons.append(
            "Very little text could be extracted directly; "
            "OCR-based parsing and vision enrichment recommended."
        )
        return ExecutionProfile.ADVANCED, tuple(reasons)

    multimodal: list[str] = []
    if has_images:
        multimodal.append("images")
    if has_tables:
        multimodal.append("tables")
    if multimodal:
        reasons.append(
            f"Document contains {' and '.join(multimodal)}; "
            "enrichment is recommended for better queries."
        )
        if page_count is not None and page_count > 100:
            reasons.append(
                "Document is long (>100 pages); confirm advanced "
                "explicitly — full enrichment may be slow."
            )
            return ExecutionProfile.STANDARD, tuple(reasons)
        return ExecutionProfile.ADVANCED, tuple(reasons)

    reasons.append(
        "Document is text-only; standard mode is the balanced choice."
    )
    if page_count is not None and page_count > 100:
        reasons.append(
            "Long document — standard avoids the cost of full enrichment."
        )
    return ExecutionProfile.STANDARD, tuple(reasons)


# ---- Capability recommendation (the new operator-facing surface) ---


# Stable wire-string values for ``CapabilityRecommendation.confidence``.
# The FE picker uses these to decide pre-check behaviour:
# ``high`` → pre-check the box; ``medium`` → show the suggestion but
# leave the box unchecked; ``low`` → no pre-check.
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


# Filename / title keyword hints — cheap deterministic signals
# operators recognise. Used to bump confidence on the
# corresponding capability when the filename or title contains a
# keyword. Lowercased substring match; very simple on purpose —
# the assessment is a recommendation layer, not a classifier.
_IMAGE_FILENAME_HINTS: frozenset[str] = frozenset({
    "image", "figure", "diagram", "scan", "scanned", "screenshot",
    "drawing", "blueprint", "photo", "visual", "chart",
})
_TABLE_FILENAME_HINTS: frozenset[str] = frozenset({
    "table", "schedule", "matrix", "quantity", "boq",
    "bill-of-quantities", "bill_of_quantities",
    "estimate", "pricing", "invoice", "cost", "item-list",
    "report-table", "specification-table",
})
_EQUATION_FILENAME_HINTS: frozenset[str] = frozenset({
    "formula", "equation", "calculation", "math", "physics",
    "load-calculation", "structural-calculation",
    "stress", "moment", "beam", "force", "coefficient", "derivation",
})


# Empty default for the per-caller domain-keyword set. The
# recommender accepts a caller-supplied ``domain_keywords``
# argument so the matching list can be sourced from the active
# domain pack at request time (see ``j1.domains.registry``).
# Core stays domain-neutral — the architectural guard at
# ``tests/extension/test_guards.py`` enforces this. Callers
# concerned with a particular vertical (e.g. construction
# documents) plug their vocabulary in via the pack registry.
_DEFAULT_DOMAIN_KEYWORDS: frozenset[str] = frozenset()


# Equation-like symbols. A document body containing several of
# these in a small sample is a cheap signal that equations are
# present. Limited set — no LaTeX detection, no full parse.
_EQUATION_SYMBOLS: tuple[str, ...] = (
    "=", "+", "-", "×", "÷", "√", "∑", "∫", "π", "≤", "≥",
    "≈", "Δ", "σ", "τ", "θ",
)


@dataclass(frozen=True)
class CapabilityRecommendation:
    """Per-capability recommendation surfaced to the FE picker.

    The FE renders three checkboxes (Process images / Process
    tables / Process equations). Each carries:

      * ``recommended`` — boolean default value for the checkbox.
        The FE pre-checks only when this is True AND ``confidence``
        is ``"high"``.
      * ``confidence`` — ``"high"`` / ``"medium"`` / ``"low"``.
        High confidence pre-checks; medium shows the suggestion
        but doesn't auto-check; low never pre-checks.
      * ``sources`` — list of signal names that drove the
        recommendation (e.g. ``"text_extractable_ratio"``,
        ``"filename_hint"``, ``"domain_hint"``). Operator-facing
        provenance.
      * ``reasons`` — operator-readable explanations, one per
        contributing signal. Shown next to the checkbox.

    The user can override before indexing; the override wins. When
    a user disables a ``high``-confidence recommendation, the
    workflow records an override warning (see
    ``AssessmentPlan.override_warnings``).
    """

    recommended: bool
    confidence: str = CONFIDENCE_LOW
    sources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "recommended": self.recommended,
            "confidence": self.confidence,
            "sources": list(self.sources),
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "CapabilityRecommendation":
        confidence = str(payload.get("confidence") or CONFIDENCE_LOW)
        if confidence not in {CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW}:
            confidence = CONFIDENCE_LOW
        return cls(
            recommended=bool(payload.get("recommended", False)),
            confidence=confidence,
            sources=tuple(
                str(s) for s in (payload.get("sources") or ())
            ),
            reasons=tuple(
                str(r) for r in (payload.get("reasons") or ())
            ),
        )


@dataclass(frozen=True)
class CapabilityRecommendations:
    """The three lightweight-assessment recommendations the FE
    picker consumes. One per capability checkbox.

    Plus ``domain_hints`` — domain-keyword names detected from
    filename/title. Surfaced verbatim so the FE can render
    "we noticed this looks like a BOQ" copy without re-deriving.
    """

    image_processing: CapabilityRecommendation
    table_processing: CapabilityRecommendation
    equation_processing: CapabilityRecommendation
    domain_hints: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "image_processing": self.image_processing.to_payload(),
            "table_processing": self.table_processing.to_payload(),
            "equation_processing": (
                self.equation_processing.to_payload()
            ),
            "domain_hints": list(self.domain_hints),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "CapabilityRecommendations":
        return cls(
            image_processing=CapabilityRecommendation.from_payload(
                payload.get("image_processing") or {},
            ),
            table_processing=CapabilityRecommendation.from_payload(
                payload.get("table_processing") or {},
            ),
            equation_processing=CapabilityRecommendation.from_payload(
                payload.get("equation_processing") or {},
            ),
            domain_hints=tuple(
                str(h) for h in (payload.get("domain_hints") or ())
            ),
        )


def _normalised_filename_hints(filename: str | None) -> str:
    """Lowercase + replace separators so hint matching is
    sub-string friendly without false-positive matches on
    e.g. ``"boq"`` inside an unrelated string. Empty when
    filename is None / empty."""
    if not filename:
        return ""
    base = filename.lower()
    for sep in ("/", "\\", ".", "_", "-", " "):
        base = base.replace(sep, "-")
    return f"-{base}-"


def _filename_matches(
    filename_norm: str, keywords: frozenset[str],
) -> tuple[str, ...]:
    """Return the keywords that appear in the normalised filename
    surface. Sorted for stable test output."""
    matches: list[str] = []
    for kw in keywords:
        if f"-{kw}-" in filename_norm or kw in filename_norm:
            matches.append(kw)
    return tuple(sorted(matches))


def _domain_hints_for(filename: str | None) -> tuple[str, ...]:
    norm = _normalised_filename_hints(filename)
    return _filename_matches(norm, _DEFAULT_DOMAIN_KEYWORDS)


def _count_equation_symbols(sample_text: str | None) -> int:
    """Cheap count of equation-like symbols in a sample of the
    document body. Caller supplies a SAMPLE — we don't pull the
    whole document. ``None`` / empty → 0."""
    if not sample_text:
        return 0
    return sum(sample_text.count(sym) for sym in _EQUATION_SYMBOLS)


def recommend_capabilities_from_assessment(
    *,
    has_images: bool,
    has_tables: bool,
    has_scanned_pages: bool,
    text_extractable_ratio: float | None,
    page_count: int | None,  # noqa: ARG001 — reserved for future heuristics
    filename: str | None = None,
    sample_text: str | None = None,
    domain_keywords: frozenset[str] | None = None,
    domain_capability_hints: "DomainAssessmentCapabilityHints | None" = None,
) -> CapabilityRecommendations:
    """Lightweight, conservative defaults for the three checkboxes.

    The recommendation is what the FE pre-checks (when confidence
    is ``high``); the user can override before indexing. Rules
    are intentionally simple — the assessment is not the
    orchestrator and shouldn't pretend to perfect knowledge.

    Additive signals:

      * ``filename`` — lowercased substring match against the
        per-capability hint sets. Bumps confidence when present.
      * ``sample_text`` — small slice of the document body used
        for the equation-symbol heuristic. None disables the
        signal; the assessment is honest about not knowing.
      * ``domain_keywords`` — caller-supplied vocabulary (e.g.
        from the active ``DomainPack.keyword_signals``). Hits
        bump confidence and surface as ``domain_hint`` sources.
        Defaults to the empty set; core stays domain-neutral.
      * ``domain_capability_hints`` — per-document-type opinions
        from the active domain pack (e.g. "BOQ documents are
        usually table-dense → recommend `process_tables`"). When
        the hint says ``recommended=True && confidence='high'``,
        that ALONE is enough to elevate the capability to high
        confidence — the operator named the file
        ``boq_2024.pdf`` and the domain knows BOQs need tables.
        Medium-confidence hints count as one contributing source.
        Hints never SUPPRESS positive signals — they only add.
    """
    filename_norm = _normalised_filename_hints(filename)
    if domain_keywords is None:
        domain_keywords = _DEFAULT_DOMAIN_KEYWORDS
    domain_matches = _filename_matches(filename_norm, domain_keywords)

    # Per-capability domain hints. None when no active pack or no
    # document type was detected. Each branch below threads the
    # corresponding hint into `_build_capability_rec`.
    image_hint = (
        domain_capability_hints.process_images
        if domain_capability_hints is not None else None
    )
    table_hint = (
        domain_capability_hints.process_tables
        if domain_capability_hints is not None else None
    )
    equation_hint = (
        domain_capability_hints.process_equations
        if domain_capability_hints is not None else None
    )

    # ---- Images -------------------------------------------------
    image_sources: list[str] = []
    image_reasons: list[str] = []
    if has_images:
        image_sources.append("has_images")
        image_reasons.append(
            "Document contains image-like content."
        )
    if has_scanned_pages:
        image_sources.append("scan_like_signal")
        image_reasons.append(
            "Scanned pages were detected — visual content "
            "likely needs extraction."
        )
    if text_extractable_ratio is not None and text_extractable_ratio < 0.1:
        image_sources.append("text_extractable_ratio")
        image_reasons.append(
            "Low extractable text ratio suggests scanned or "
            "visual content."
        )
    image_filename_hits = _filename_matches(
        filename_norm, _IMAGE_FILENAME_HINTS,
    )
    if image_filename_hits:
        image_sources.append("filename_hint")
        image_reasons.append(
            "Filename/title suggests image-heavy content "
            f"(matched: {', '.join(image_filename_hits)})."
        )
    image_rec = _build_capability_rec(
        sources=image_sources,
        reasons=image_reasons,
        no_signal_reason=(
            "No strong image, scanned-page, or low-text signal "
            "was detected."
        ),
        domain_matches=domain_matches,
        domain_hint=image_hint,
    )

    # ---- Tables -------------------------------------------------
    table_sources: list[str] = []
    table_reasons: list[str] = []
    if has_tables:
        table_sources.append("table_like_text_layout")
        table_reasons.append(
            "Document appears to contain repeated row/column "
            "structures."
        )
    table_filename_hits = _filename_matches(
        filename_norm, _TABLE_FILENAME_HINTS,
    )
    if table_filename_hits:
        table_sources.append("filename_hint")
        table_reasons.append(
            "Filename/title suggests a table-shaped document "
            f"(matched: {', '.join(table_filename_hits)})."
        )
    table_rec = _build_capability_rec(
        sources=table_sources,
        reasons=table_reasons,
        no_signal_reason=(
            "No strong table or grid signal was detected."
        ),
        domain_matches=domain_matches,
        domain_hint=table_hint,
    )

    # ---- Equations ----------------------------------------------
    equation_sources: list[str] = []
    equation_reasons: list[str] = []
    eq_symbol_count = _count_equation_symbols(sample_text)
    if eq_symbol_count >= 5:
        equation_sources.append("equation_symbol_signal")
        equation_reasons.append(
            "Document body contains formula or mathematical "
            f"symbols ({eq_symbol_count} occurrences in the "
            "sample)."
        )
    equation_filename_hits = _filename_matches(
        filename_norm, _EQUATION_FILENAME_HINTS,
    )
    if equation_filename_hits:
        equation_sources.append("filename_hint")
        equation_reasons.append(
            "Filename/title suggests formula or calculation "
            f"content (matched: {', '.join(equation_filename_hits)})."
        )
    equation_rec = _build_capability_rec(
        sources=equation_sources,
        reasons=equation_reasons,
        no_signal_reason=(
            "No strong formula or mathematical-symbol signal "
            "was detected."
        ),
        domain_matches=domain_matches,
        domain_hint=equation_hint,
    )

    return CapabilityRecommendations(
        image_processing=image_rec,
        table_processing=table_rec,
        equation_processing=equation_rec,
        domain_hints=domain_matches,
    )


def _build_capability_rec(
    *,
    sources: list[str],
    reasons: list[str],
    no_signal_reason: str,
    domain_matches: tuple[str, ...],
    domain_hint: "DomainAssessmentCapabilityHint | None" = None,
) -> CapabilityRecommendation:
    """Compose a recommendation from its accumulated signals.

    Confidence rule (deliberately conservative):

      * 0 contributing sources → ``low``, not recommended.
      * 1 source → ``medium``, recommended but not pre-checked.
      * 2+ sources OR a domain hint alongside ≥1 source →
        ``high``, recommended + pre-checked.

    Domain hint elevates a single signal to high — operators who
    name a file ``boq_2024.pdf`` are signalling intent strongly.

    ``domain_hint`` is the per-document-type opinion from the
    active pack (e.g. ``boq.process_tables``). Semantics:

      * Hint with ``recommended=True && confidence='high'`` is
        AUTHORITATIVE — alone it returns high+recommended, even
        when no other signal fired. The pack says "BOQ documents
        ALWAYS want tables" and the operator confirmed the
        document type, so the assessment defers to that.
      * Hint with ``recommended=True && confidence='medium'``
        contributes one source toward the normal confidence
        ladder.
      * ``recommended=False`` hints are silent — they neither
        suppress positive signals nor add reasons. The user can
        always uncheck.
    """
    # Domain hint, when high+recommended, overrides the source-
    # count ladder. The detected document type is a strong signal
    # the pack codifies.
    if (
        domain_hint is not None
        and domain_hint.recommended
        and domain_hint.confidence == CONFIDENCE_HIGH
    ):
        forced_sources = list(sources)
        forced_reasons = list(reasons)
        if "domain_type_hint" not in forced_sources:
            forced_sources.append("domain_type_hint")
        if domain_hint.reason:
            forced_reasons.append(domain_hint.reason)
        return CapabilityRecommendation(
            recommended=True,
            confidence=CONFIDENCE_HIGH,
            sources=tuple(forced_sources),
            reasons=tuple(forced_reasons) or (no_signal_reason,),
        )

    # Medium-confidence + recommended hint contributes one
    # source. Low / not-recommended hints are silent.
    if (
        domain_hint is not None
        and domain_hint.recommended
        and domain_hint.confidence == CONFIDENCE_MEDIUM
    ):
        sources = [*sources, "domain_type_hint"]
        if domain_hint.reason:
            reasons = [*reasons, domain_hint.reason]

    if not sources:
        return CapabilityRecommendation(
            recommended=False,
            confidence=CONFIDENCE_LOW,
            sources=(),
            reasons=(no_signal_reason,),
        )
    if len(sources) >= 2 or domain_matches:
        confidence = CONFIDENCE_HIGH
        # Surface the domain hint as a source string when it
        # contributed (so the FE picker can render "matched
        # because domain looks like X").
        if domain_matches and "domain_hint" not in sources:
            sources = [*sources, "domain_hint"]
            reasons = [*reasons, (
                f"Domain hints detected: "
                f"{', '.join(domain_matches)}."
            )]
    else:
        confidence = CONFIDENCE_MEDIUM
    return CapabilityRecommendation(
        recommended=True,
        confidence=confidence,
        sources=tuple(sources),
        reasons=tuple(reasons),
    )


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
