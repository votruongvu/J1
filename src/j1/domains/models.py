"""Domain Pack data model.

Pure dataclasses + a small protocol — no I/O, no LLM coupling. The
registry constructs `DomainPack` instances from the YAML data files
under each pack directory; the planner consumes the dataclasses.

Wire shape: every field that ends up in `domain_context` on the
persisted `planning_result.json` is a primitive (str / int / float /
bool / list / dict) so the artifact stays portable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


__all__ = [
    "DOMAIN_SELECTION_AUTO_DETECTED",
    "DOMAIN_SELECTION_FALLBACK_GENERAL",
    "DOMAIN_SELECTION_USER",
    "DOMAIN_SELECTION_WORKSPACE",
    "ENRICHMENT_POLICY_ALWAYS",
    "ENRICHMENT_POLICY_AUTO",
    "ENRICHMENT_POLICY_NEVER",
    "DomainContext",
    "DomainDetectionResult",
    "DomainEnrichmentPolicy",
    "DomainExtractionHints",
    "DomainPack",
    "DomainPlanningOverlay",
    "DomainPromptPack",
    "DomainSelectionSource",
    "DomainValidationRules",
    "KeywordSignal",
    "UnsupportedCapability",
]


# Stable wire vocabulary — used by the registry, the planner output,
# and the FE Planning Report tab. Mirrors the spec's documented set.
DOMAIN_SELECTION_USER = "user"
DOMAIN_SELECTION_WORKSPACE = "workspace"
DOMAIN_SELECTION_AUTO_DETECTED = "auto_detected"
DOMAIN_SELECTION_FALLBACK_GENERAL = "fallback_general"


# Type alias for the four-string selection-source vocabulary above.
DomainSelectionSource = str


# Domain enrichment-policy vocabulary. Stable wire strings — operator
# docs + YAML schemas refer to these by literal value.
#
# `AUTO` — the post-compile rule-based assessor decides. Domain
#  still contributes via force/optional/denied task lists.
# `ALWAYS` — upgrade the assessor's verdict to at least RECOMMENDED;
#  apply force_recommended_tasks regardless of compile
#  signals. SKIP remains honoured for blocking conditions
#  (compile failure, empty document).
# `NEVER` — collapse to SKIP unless the run already had a stronger
#  blocking reason. Disables enrichment for this domain
#  even when compile produced rich signals.
ENRICHMENT_POLICY_AUTO = "auto"
ENRICHMENT_POLICY_ALWAYS = "always"
ENRICHMENT_POLICY_NEVER = "never"

_ENRICHMENT_POLICY_VOCABULARY = frozenset({
    ENRICHMENT_POLICY_AUTO,
    ENRICHMENT_POLICY_ALWAYS,
    ENRICHMENT_POLICY_NEVER,
})


# ---- Dataclasses ----------------------------------------------------


@dataclass(frozen=True)
class KeywordSignal:
    """One keyword/phrase with a relative weight.

 Keywords are matched as case-insensitive substring against the
 detection corpus (title, headings, early-page previews, table
 captions, image captions, filename). `weight` controls how much
 a single hit contributes to the domain's confidence score.

 Strong signals: ~1.0 (BOQ, "Bill of Quantities", "Method Statement")
 Medium signals: ~0.5 (project, contractor, drawing, specification)
 Weak signals: ~0.2 (report, plan, document)

 The detector is intentionally simple — keyword catalogues are
 auditable and dramatically faster than embeddings or an LLM call.
 """

    text: str
    weight: float = 0.5
    # Optional category — cosmetic, used by the FE Planning Report
    # to group evidence ("table-header signal", "structural-element
    # term", …).
    category: str | None = None


@dataclass(frozen=True)
class DomainPlanningOverlay:
    """Per-document-type planning overrides a domain pack provides.

 The overlay sits *on top of* the generic rule-based assessment.
 `recommended_profile` (when set) supersedes the generic pick.
 `step_overrides` is a step_name → {enabled, scope, reason, …}
 dict that the planner merges into the rule-based execution plan.

 Unset fields fall through to the generic decision — the domain
 pack only weighs in where it has stronger evidence than the
 generic heuristics."""

    document_type: str  # the type this overlay applies to
    recommended_profile: str | None = None
    chunking_strategy: str | None = None
    step_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    extraction_targets: tuple[str, ...] = ()
    candidate_entity_types: tuple[str, ...] = ()
    applied_rule_id: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class UnsupportedCapability:
    """Capability the domain pack would have liked to use but which
 the framework / deployment doesn't currently implement.

 Recorded on the planning result so reviewers see *intent* (e.g.
 "the civil pack wanted action-item extraction") even when the
 pipeline can't deliver it yet. Lets future work pick up the
 backlog without re-deriving requirements."""

    capability: str
    reason: str


@dataclass(frozen=True)
class DomainContext:
    """The post-compile planner attaches one of these to every
 `planning_result.json`. `selected_domain` is the pack id; an
 inactive run still gets a context with `selected_domain="general"`
 so consumers don't have to special-case its absence."""

    selected_domain: str
    selection_source: DomainSelectionSource
    confidence: float
    domain_pack_version: str
    evidence: tuple[str, ...] = ()
    applied_domain_rules: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    recommended_but_unsupported: tuple[UnsupportedCapability, ...] = ()
    # Detector breakdown — auditable per-candidate confidence used
    # by `select_domain` to pick the winner. Empty when no detection
    # ran (operator override path).
    candidates: tuple["DomainDetectionResult", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_domain": self.selected_domain,
            "selection_source": self.selection_source,
            "confidence": self.confidence,
            "domain_pack_version": self.domain_pack_version,
            "evidence": list(self.evidence),
            "applied_domain_rules": list(self.applied_domain_rules),
            "warnings": list(self.warnings),
            "recommended_but_unsupported": [
                {"capability": u.capability, "reason": u.reason}
                for u in self.recommended_but_unsupported
            ],
            "candidates": [
                {
                    "domain_id": c.domain_id,
                    "confidence": c.confidence,
                    "evidence": list(c.evidence),
                }
                for c in self.candidates
            ],
        }


@dataclass(frozen=True)
class DomainDetectionResult:
    """Per-candidate detection score. Carried inside `DomainContext`
 so reviewers can see why a given domain won (or lost)."""

    domain_id: str
    confidence: float
    evidence: tuple[str, ...] = ()
    detected_document_type: str | None = None
    applied_rule_id: str | None = None
    overlay: DomainPlanningOverlay | None = None


# A `DetectionFn` takes the same inputs as the post-compile planner
# (document metadata, manifest, digest, generic understanding) and
# returns the domain's score + evidence + (optional) detected type.
DetectionFn = Callable[..., DomainDetectionResult]


@dataclass(frozen=True)
class DomainEnrichmentPolicy:
    """How a domain pack wants the post-compile assessor to handle
 enrichment for documents under its selection.

 Lets a domain say "this kind of work is meaningless without
 enrichment" (policy=always + force_recommended_tasks=[…]) or
 "we never enrich this domain" (policy=never) without the core
 rule-based assessor needing to grow domain-specific branches.

 Field semantics:
 * `policy` — one of `auto` / `always` / `never`. Drives the
 verdict upgrade/downgrade in `assess_post_compile_enrich`.
 * `force_recommended_tasks` — task ids the pack always wants
 recommended whenever enrichment runs. Bypasses the
 per-signal heuristics (e.g. a civil pack always wants
 requirement extraction even when no tables/images were
 detected).
 * `optional_tasks` — additional tasks the pack suggests when
 signals are ambiguous. Recommended only when other rule-
 based recommendations exist; never standalone.
 * `denied_tasks` — tasks the pack opts OUT of even when
 compile signals would otherwise recommend them (e.g. a
 domain with regulated data may want image_captioning OFF).
 * `require_enrichment_success` — when True, downstream
 workflow treats an enrichment failure as a run failure
 instead of a warning. Workflow consumes the flag via the
 request, not via this policy directly; the field is
 surfaced here so the FE shows the operator the
 domain-stated requirement alongside the run setting.
 * `default_model_tier` — `fast` / `premium` / `vision` hint
 for downstream model-selection helpers. None = inherit from
 deployment default.
 * `reasoning` — one-sentence operator-readable explanation
 that lands on the persisted plan (`reasons` tuple) when
 this policy influences the verdict.
 """

    policy: str = ENRICHMENT_POLICY_AUTO
    force_recommended_tasks: tuple[str, ...] = ()
    optional_tasks: tuple[str, ...] = ()
    denied_tasks: tuple[str, ...] = ()
    require_enrichment_success: bool = False
    default_model_tier: str | None = None
    reasoning: str = ""

    def __post_init__(self) -> None:
        if self.policy not in _ENRICHMENT_POLICY_VOCABULARY:
            raise ValueError(
                f"unknown enrichment policy {self.policy!r}; expected one "
                f"of {sorted(_ENRICHMENT_POLICY_VOCABULARY)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection. Used to embed the policy on the
 persisted post-compile-enrich plan + planning result so the
 FE can render "Domain policy: always" alongside the verdict."""
        return {
            "policy": self.policy,
            "force_recommended_tasks": list(self.force_recommended_tasks),
            "optional_tasks": list(self.optional_tasks),
            "denied_tasks": list(self.denied_tasks),
            "require_enrichment_success": self.require_enrichment_success,
            "default_model_tier": self.default_model_tier,
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True)
class DomainExtractionHints:
    """Per-domain extraction hints the enrichers will consume.

 Pure data. Generic and domain-agnostic — concrete packs populate
 the lists with their vocabulary; the enricher reads them through
 this interface so no domain-specific branches leak into core
 code.

 Field semantics:
 * `metadata_fields` — operator-facing metadata keys the
 domain wants extracted (e.g. ``project_number``,
 ``drawing_revision``). The metadata enricher reads this
 list and asks the LLM for those keys specifically.
 * `entity_hints` — entity types the domain expects (e.g.
 ``Contractor``, ``StructuralElement``, ``InspectionFinding``).
 * `table_hints` — operator-readable cues for table
 interpretation (e.g. "BOQ tables have item/description/
 unit/quantity columns").
 * `image_hints` — cues for image interpretation (e.g. "site
 photos may show defects; drawings should be inspected for
 revision boxes").
 * `terminology_hints` — domain glossary entries / synonym
 pairs that retrieval + classification should normalise on.
 * `retrieval_hints` — phrasing/lookup tips the indexer can
 encode as additional keys ("query for 'RFI'/'request for
 information' should match both").

 All fields default to empty tuples / dicts so a pack that
 doesn't populate a category is a no-op for that enricher."""

    metadata_fields: tuple[str, ...] = ()
    entity_hints: tuple[str, ...] = ()
    table_hints: tuple[str, ...] = ()
    image_hints: tuple[str, ...] = ()
    terminology_hints: tuple[str, ...] = ()
    retrieval_hints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata_fields": list(self.metadata_fields),
            "entity_hints": list(self.entity_hints),
            "table_hints": list(self.table_hints),
            "image_hints": list(self.image_hints),
            "terminology_hints": list(self.terminology_hints),
            "retrieval_hints": list(self.retrieval_hints),
        }


@dataclass(frozen=True)
class DomainValidationRules:
    """Per-domain validation rules the post-compile analyzer +
 validation enricher consume.

 Pure data — no validation logic lives on the dataclass. The
 rules describe WHAT the domain considers required / suspicious;
 consumers decide how to enforce them.

 Field semantics:
 * `required_metadata_fields` — metadata keys the domain
 REQUIRES on every document. A missing field surfaces as a
 validation warning + biases the post-compile analyzer
 toward recommending metadata enrichment.
 * `expected_document_structure` — operator-readable phrases
 describing the structure the domain expects (e.g.
 "method statements should have a 'Procedure' section").
 Surfaced on the FE as a checklist; not auto-asserted.
 * `low_quality_warning_conditions` — triggers that flag a
 compile result as low-quality even when the parser's
 verdict is "good" (e.g. "page_count > 50 but
 total_text_chars < 5000"). Consumed by
 `assess_post_compile_enrich` as additional warning sources.
 * `enrichment_triggers` — conditions that should force-
 recommend enrichment regardless of compile signals (e.g.
 "document_type == method_statement → require risk
 extraction"). Pack-side description; the analyzer
 evaluates them via the existing rule chain.

 All fields default to empty so a pack that doesn't populate
 them is a no-op."""

    required_metadata_fields: tuple[str, ...] = ()
    expected_document_structure: tuple[str, ...] = ()
    low_quality_warning_conditions: tuple[str, ...] = ()
    enrichment_triggers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_metadata_fields": list(self.required_metadata_fields),
            "expected_document_structure": list(self.expected_document_structure),
            "low_quality_warning_conditions": list(self.low_quality_warning_conditions),
            "enrichment_triggers": list(self.enrichment_triggers),
        }


@dataclass(frozen=True)
class DomainPromptPack:
    """Per-domain prompt template strings consumed by enrichers.

 Pure data — no LLM coupling. Each field is the prompt text the
 matching enricher prepends to its system message; None means
 "use the enricher's built-in default".

 Distinct from `DomainPack.prompt_addon`:
 * `prompt_addon` is a one-paragraph DOMAIN-WIDE addendum
 prepended to EVERY enricher's prompt for context.
 * `DomainPromptPack` holds PER-ENRICHER overrides. A pack can
 replace just the table prompt, for example, leaving the
 rest to defaults.

 Field semantics mirror the enricher kinds in `j1.enrichers`:
 * `text_enrichment_prompt` — generic text-enrichment override.
 * `metadata_enrichment_prompt` — metadata extraction.
 * `table_enrichment_prompt` — table interpretation.
 * `image_enrichment_prompt` — vision / image captioning.
 * `classification_prompt` — document classification.
 * `validation_prompt` — domain-rule validation enricher.

 All fields default to None (no override)."""

    text_enrichment_prompt: str | None = None
    metadata_enrichment_prompt: str | None = None
    table_enrichment_prompt: str | None = None
    image_enrichment_prompt: str | None = None
    classification_prompt: str | None = None
    validation_prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text_enrichment_prompt": self.text_enrichment_prompt,
            "metadata_enrichment_prompt": self.metadata_enrichment_prompt,
            "table_enrichment_prompt": self.table_enrichment_prompt,
            "image_enrichment_prompt": self.image_enrichment_prompt,
            "classification_prompt": self.classification_prompt,
            "validation_prompt": self.validation_prompt,
        }


@dataclass(frozen=True)
class DomainPack:
    """One Domain Pack.

 Built once at startup by `DomainRegistry`. Pure data + a single
 detection callable; everything else is reference-only and
 surfaced to the planner / FE.

 `extends_document_types` is the pack's contribution to the wire-
 schema's allowed `document_type` set. Generic types (the 25
 entries in `DOCUMENT_TYPES`) are always allowed; pack-extended
 types are accepted whenever the matching pack is registered.

 `prompt_addon` is appended to the LLM planner's system prompt
 when this pack is selected AND `J1_LLM_PLANNING_ENABLED=true`.
 """

    id: str
    display_name: str
    version: str
    extends_document_types: tuple[str, ...] = ()
    keyword_signals: tuple[KeywordSignal, ...] = ()
    extraction_targets: tuple[str, ...] = ()
    graph_entity_types: tuple[str, ...] = ()
    graph_relationship_types: tuple[str, ...] = ()
    prompt_addon: str = ""
    overlays: dict[str, DomainPlanningOverlay] = field(default_factory=dict)
    # Capabilities the pack would use but aren't supported in the
    # current framework — surfaced to the planning result so the
    # backlog stays visible.
    unsupported_capabilities: tuple[UnsupportedCapability, ...] = ()
    # How this domain wants the post-compile assessor to handle
    # enrichment. Defaults to auto-no-overrides — the rule-based
    # assessor still drives the verdict, the pack just doesn't
    # contribute force/optional/denied lists. Packs that need
    # domain-mandatory tasks (civil's requirement_extraction, etc.)
    # populate `force_recommended_tasks` here.
    enrichment_policy: DomainEnrichmentPolicy = field(
        default_factory=DomainEnrichmentPolicy,
    )
    # Per-domain extraction hints the enrichers will consume.
    # Defaults to empty so generic / unconfigured packs are no-op.
    # The civil pack populates metadata_fields, entity_hints, table/
    # image hints, and terminology entries; the analyzer + enrichers
    # read these through the typed interface only.
    extraction_hints: DomainExtractionHints = field(
        default_factory=DomainExtractionHints,
    )
    # Per-domain validation rules the post-compile analyzer +
    # validation enricher consume. Defaults to empty; populated
    # packs surface required metadata fields, expected structure,
    # extra low-quality conditions, and enrichment triggers.
    validation_rules: DomainValidationRules = field(
        default_factory=DomainValidationRules,
    )
    # Per-enricher prompt overrides (table / image / metadata /
    # classification / validation / text). None means "use the
    # enricher's built-in default". Sits alongside `prompt_addon`
    # (the domain-wide addendum): the addon is appended to EVERY
    # prompt; the prompt-pack fields REPLACE the per-enricher
    # default when set.
    prompt_pack: DomainPromptPack = field(
        default_factory=DomainPromptPack,
    )
    # Detection function. Generic pack uses a no-op; civil pack
    # uses a keyword + structural scorer.
    detect: DetectionFn | None = None


class DetectionContext(Protocol):
    """Inputs the detection function receives.

 Documented as a Protocol (not a dataclass) so future packs can
 extend without breaking earlier ones — a pack only reads the
 attributes it actually needs."""

    title: str
    title_quality: str
    filename: str | None
    early_page_text: str
    heading_outline: tuple[tuple[int, str, int | None], ...]
    table_captions: tuple[str, ...]
    image_captions: tuple[str, ...]
    document_type_hint: str | None  # generic detector's call
