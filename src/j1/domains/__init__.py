"""J1 Domain Packs.

Domain packs are pluggable bundles that extend the generic post-
compile planner with domain-aware decisions. Each pack supplies:

 * a stable id (`general`, `civil_engineering`, …)
 * an extended document-type taxonomy
 * keyword + structural detection rules
 * per-document-type planning rules
 * an extraction-target catalogue (planning hints)
 * a graph ontology (entity + relationship types)
 * an optional LLM prompt addon

Selection order, applied at planning time:

 1. Per-run override (operator/upload-time override).
 2. Workspace / project default domain.
 3. Auto-detection against the document content.
 4. Generic fallback (`general`).

Auto-detection uses a confidence threshold so weak signals can't
force a domain. Operator-forced selections are honored even when
the evidence is weak; a warning records the override.

The generic planner is the always-available baseline — domain packs
*augment* it, never replace it.
"""

from j1.domains.models import (
    DomainContext,
    DomainDetectionResult,
    DomainPack,
    DomainPlanningOverlay,
    DomainSelectionSource,
    KeywordSignal,
    UnsupportedCapability,
)
from j1.domains.registry import (
    DOMAIN_GENERAL,
    DomainRegistry,
    default_registry,
)


__all__ = [
    "DOMAIN_GENERAL",
    "DomainContext",
    "DomainDetectionResult",
    "DomainPack",
    "DomainPlanningOverlay",
    "DomainRegistry",
    "DomainSelectionSource",
    "KeywordSignal",
    "UnsupportedCapability",
    "default_registry",
]
