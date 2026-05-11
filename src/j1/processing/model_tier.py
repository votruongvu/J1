"""model tier selection helper.

Resolves which LLM tier (`fast` / `premium` / `vision`) an
enrichment module should use, given:

 * the deployment default tier (from `EnrichmentConcurrencySettings`),
 * the domain pack's `DomainEnrichmentPolicy.default_model_tier`,
 * the post-compile plan's `model_tier_selection`,
 * the module's input requirements (e.g. an image enricher must
 use the vision tier or skip),
 * whether the compile result actually has image content.

The selection is PURE — no LLM call, no vendor client lookup.
Returns a `ModelTierDecision` carrying the selected tier + the
reason so the FE / audit log can render "using vision because the
compile result has 4 images".

Precedence (highest first):
 1. Module requirement (a vision enricher MUST have vision or
 the decision is `SKIPPED`).
 2. Post-compile plan's `model_tier_selection` (operator/policy
 override that landed on the analyzer's output).
 3. Domain policy default (`DomainEnrichmentPolicy.default_model_tier`).
 4. System default (`EnrichmentConcurrencySettings.default_model_tier`).

Vision gating: `MODEL_TIER_VISION` requires image content in the
compile result. A non-vision module asking for vision falls back
to `premium` (most capable text-only tier). A vision module
asking for fast/premium overrides the request to vision if image
content exists, otherwise the decision is `skip_no_image_content`."""

from __future__ import annotations

from dataclasses import dataclass

from j1.processing.compile_result import NormalizedCompileResult
from j1.processing.enrichment_settings import (
    EnrichmentConcurrencySettings,
    MODEL_TIER_FAST,
    MODEL_TIER_PREMIUM,
    MODEL_TIER_VISION,
)


__all__ = [
    "ModelTierDecision",
    "MODULE_REQUIREMENT_NONE",
    "MODULE_REQUIREMENT_TEXT",
    "MODULE_REQUIREMENT_VISION",
    "select_model_tier",
]


# Module-side requirement vocabulary. The module says what KIND of
# input it processes; the selector decides which tier to use.
MODULE_REQUIREMENT_NONE = "none"
MODULE_REQUIREMENT_TEXT = "text"
MODULE_REQUIREMENT_VISION = "vision"

_VALID_TIERS = frozenset({
    MODEL_TIER_FAST, MODEL_TIER_PREMIUM, MODEL_TIER_VISION,
})


@dataclass(frozen=True)
class ModelTierDecision:
    """The selector's typed verdict.

 `selected_tier` is the tier the caller should use; None means
 the call should be SKIPPED (e.g. vision module on a doc with
 no images). `reason` is operator-readable provenance the FE +
 final report render alongside the tier."""

    selected_tier: str | None
    reason: str
    requested_tier: str | None = None
    skipped: bool = False


def select_model_tier(
    *,
    settings: EnrichmentConcurrencySettings,
    module_requirement: str = MODULE_REQUIREMENT_TEXT,
    domain_default_tier: str | None = None,
    plan_tier_selection: str | None = None,
    compile_result: NormalizedCompileResult | None = None,
) -> ModelTierDecision:
    """Resolve the LLM tier for one module call.

 See module docstring for precedence + vision gating rules.
 Pure / no I/O — same inputs → same decision."""
    requested = _first_valid_tier(
        plan_tier_selection,
        domain_default_tier,
        settings.default_model_tier,
    )

    if module_requirement == MODULE_REQUIREMENT_VISION:
        has_images = _has_image_content(compile_result)
        if not has_images:
            return ModelTierDecision(
                selected_tier=None,
                reason=(
                    "vision module skipped: compile result has no "
                    "image content"
                ),
                requested_tier=requested,
                skipped=True,
            )
        return ModelTierDecision(
            selected_tier=MODEL_TIER_VISION,
            reason="vision module requires vision tier",
            requested_tier=requested,
        )

    # Text-input module asked for vision → fall back to premium
    # (most capable text-only). Don't silently use vision on
    # non-vision content.
    if requested == MODEL_TIER_VISION and module_requirement != MODULE_REQUIREMENT_VISION:
        return ModelTierDecision(
            selected_tier=MODEL_TIER_PREMIUM,
            reason=(
                "requested vision tier; module input is text — "
                "falling back to premium"
            ),
            requested_tier=requested,
        )

    return ModelTierDecision(
        selected_tier=requested,
        reason=f"using {requested} tier per resolved precedence",
        requested_tier=requested,
    )


# ---- Helpers ------------------------------------------------------


def _first_valid_tier(*candidates: str | None) -> str:
    """Walk a precedence chain of tier strings; return the first
 valid value. Falls back to `MODEL_TIER_FAST` if every
 candidate is None / invalid (guards against malformed policy
 fields)."""
    for c in candidates:
        if isinstance(c, str) and c.strip().lower() in _VALID_TIERS:
            return c.strip().lower()
    return MODEL_TIER_FAST


def _has_image_content(
    compile_result: NormalizedCompileResult | None,
) -> bool:
    """True when the compile output surfaced any image content.

 Treats the absence of `compile_result` (e.g. caller didn't
 thread it through) as "no image content" rather than raising —
 conservative for vision-tier gating."""
    if compile_result is None:
        return False
    if compile_result.detected_images:
        return True
    if "images" in compile_result.detected_content_types:
        return True
    return False
