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
    "DomainContext",
    "DomainDetectionResult",
    "DomainPack",
    "DomainPlanningOverlay",
    "DomainSelectionSource",
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


# ---- Dataclasses ----------------------------------------------------


@dataclass(frozen=True)
class KeywordSignal:
    """One keyword/phrase with a relative weight.

    Keywords are matched as case-insensitive substring against the
    detection corpus (title, headings, early-page previews, table
    captions, image captions, filename). `weight` controls how much
    a single hit contributes to the domain's confidence score.

    Strong signals: ~1.0  (BOQ, "Bill of Quantities", "Method Statement")
    Medium signals: ~0.5  (project, contractor, drawing, specification)
    Weak signals:   ~0.2  (report, plan, document)

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
