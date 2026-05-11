"""Map a vendor-neutral `AssessmentPlan` to RAGAnything compile config.

The mapper is the ONLY place RAGAnything-specific knowledge of the
assessment plan lives. Replacing RAGAnything with another compiler
later means swapping this mapper module — the core
`AssessmentPlan` contract stays untouched.

Mapping summary:

  fast      → parse_method="txt"
              image / formula processing OFF unless explicitly
              required by the plan.
              table processing only when required.

  standard  → parse_method="auto"
              image / table / formula processing follow the plan's
              required_capabilities + optional_capabilities.

  deep      → parse_method="ocr" when the plan requires `OCR`,
              otherwise "auto".
              image / table / formula processing all enabled.
              Quality warnings recorded for later optimisation.

When a capability the plan requires is not supported by the current
RAGAnything install (per `RAGAnythingSettings.supports_*` defaults
or the `J1_RAGANYTHING_SUPPORTS_*` env), the mapper records a
warning and either degrades gracefully (default fallback policy)
or raises `CompileCapabilityUnsupported` (FAIL policy). The compile
result keeps these warnings so a later optimisation pass can
re-route the document to a different adapter.

Env precedence (final runtime decision order):

    AssessmentPlan
    > project / profile policy (not yet implemented)
    > adapter defaults (this module's tables)
    > env defaults (RAGAnythingSettings allowed_parse_methods etc.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from j1.processing.assessment import (
    AssessmentPlan,
    Capability,
    CompileMode,
    FallbackPolicy,
)
from j1.providers.raganything.settings import (
    RAGAnythingSettings,
    VALID_PARSE_METHODS,
)

# Map CompileMode → preferred parse_method. Two-mode model:
# `standard` and `deep` are the only modes the planner emits. The
# adapter resolves parse_method here from the mode + capability set
# (e.g. `deep` promotes to "ocr" when the plan requires OCR).
#
# `FAST → txt` is kept as a LEGACY fallback so adapters can still
# resolve a parse_method when reading an old AssessmentPlan from a
# historical artifact. The planner itself never emits FAST any more
# (see assessment.py); the safety belt coerces FAST → STANDARD on
# the read path before this mapping is consulted in the normal
# flow. Touch this dict together with `CompileMode` if either
# changes.
_MODE_TO_PARSE_METHOD: dict[CompileMode, str] = {
    CompileMode.FAST: "txt",   # legacy round-trip only — never emitted
    CompileMode.STANDARD: "auto",
    CompileMode.DEEP: "auto",  # promoted to "ocr" when plan requires OCR
}


class CompileCapabilityUnsupported(Exception):
    """Raised when an `AssessmentPlan` requires a capability the
    underlying parser doesn't support AND the plan's fallback policy
    is `FAIL`. Caller (compile activity) maps this to a stage
    failure with the missing capability in the failure_message."""

    def __init__(self, capability: Capability, message: str) -> None:
        super().__init__(message)
        self.capability = capability


@dataclass(frozen=True)
class CompileConfig:
    """RAGAnything-specific compile config derived from an
    AssessmentPlan. Output is intentionally split into TWO buckets
    that travel different paths to RAGAnything:

      * `parser_kwargs` — values forwarded to
        `process_document_complete(...)`. Today: only `parse_method`.
        (Future: `lang`, `device`, `start_page`, etc. as RAGAnything
        exposes them at the parser level.)

      * `config_overrides` — values set on the `RAGAnythingConfig`
        object before the `RAGAnything` instance is constructed.
        Today: `enable_image_processing`, `enable_table_processing`,
        `enable_equation_processing` — the per-capability toggles
        RAGAnything's CONFIG layer accepts. The bridge applies them
        defensively (only the fields the installed `RAGAnythingConfig`
        actually exposes get set), so a vendor version that drops
        a flag still works.

    This split matters because RAGAnything's `process_document_complete`
    does NOT take `enable_image_processing` etc. as kwargs — those are
    config-level, not call-level. Mixing them at the call site was the
    earlier-iteration mistake that the docstring + tests now guard
    against.

    `warnings` + `unhandled_capabilities` carry the "couldn't honour
    the plan" signal. With the per-capability flags now applied at
    the config level, these only fire when the deployment EXPLICITLY
    marks a capability unsupported via `J1_RAGANYTHING_SUPPORTS_*`,
    or for capabilities (OCR) that ride on parse_method.
    """

    parse_method: str
    enable_image_processing: bool
    enable_table_processing: bool
    enable_equation_processing: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
    unhandled_capabilities: tuple[str, ...] = field(default_factory=tuple)
    # The mode the mapper resolved to — useful for logging /
    # debugging. Same value as plan.mode in nearly all cases.
    resolved_mode: str = ""

    def to_parser_kwargs(self) -> dict[str, object]:
        """Kwargs forwarded to `RAGAnything.process_document_complete`.

        Today this is `{parse_method}`. As RAGAnything exposes more
        parser-level args (`lang`, `device`, `start_page`, …) the
        mapper grows the dict; the bridge call site stays
        `**parser_kwargs` so it doesn't need to enumerate them.
        """
        return {"parse_method": self.parse_method}

    def to_config_overrides(self) -> dict[str, object]:
        """Field-name → value dict for `RAGAnythingConfig`. The
        bridge applies these defensively (`setattr` only when the
        installed version exposes the field) so a vendor-side
        rename surfaces as a warning, not a crash."""
        return {
            "enable_image_processing": self.enable_image_processing,
            "enable_table_processing": self.enable_table_processing,
            "enable_equation_processing": self.enable_equation_processing,
        }


def map_assessment_to_raganything_config(
    plan: AssessmentPlan,
    settings: RAGAnythingSettings,
) -> CompileConfig:
    """Translate an AssessmentPlan into a `CompileConfig` for the
    RAGAnything bridge.

    Honours the plan's `mode` + `required_capabilities` first.
    Falls back to `settings` only for fields the plan doesn't
    speak to (currently: nothing — `settings.parse_method` is
    deliberately ignored in favour of the mode-derived value, since
    the plan supersedes env at compile time).

    Raises `CompileCapabilityUnsupported` only when the plan's
    `fallback_policy=FAIL` AND a required capability isn't
    serviceable. Default policy degrades with warnings.
    """
    warnings: list[str] = []

    # ---- parse_method ---------------------------------------------
    parse_method = _MODE_TO_PARSE_METHOD[plan.mode]
    if plan.mode == CompileMode.DEEP and plan.requires(Capability.OCR):
        # Deep + OCR required → force OCR explicitly. The "auto"
        # default would still hit OCR for scanned pages, but
        # explicit is better here: operators reading the audit log
        # see WHY the slow path was chosen.
        parse_method = "ocr"

    # Env-defined allow-list. When the operator constrains the
    # allowed methods (e.g. `J1_RAGANYTHING_ALLOWED_PARSE_METHODS=
    # auto,txt`) and the plan picked a method outside it, fall
    # back to the deployment default. Defensive — the env is the
    # operator's escape hatch when the adapter would otherwise
    # invoke something the deployment can't actually run (e.g. an
    # OCR backend the worker doesn't have).
    allowed = _allowed_parse_methods(settings)
    if parse_method not in allowed:
        replacement = (
            settings.parse_method if settings.parse_method in allowed
            else next(iter(sorted(allowed)), "auto")
        )
        warnings.append(
            f"plan requested parse_method={parse_method!r} but "
            f"deployment allow-list is {sorted(allowed)!r}; "
            f"falling back to {replacement!r}"
        )
        parse_method = replacement

    if parse_method not in VALID_PARSE_METHODS:
        # Final safety net — should never trip if settings/env validate.
        parse_method = "auto"

    # ---- per-capability toggles ----------------------------------
    # Defaults derived from the mode; required_capabilities
    # promotes ON; optional_capabilities is mode-dependent.
    enable_image = _capability_enabled(
        plan, Capability.IMAGE_EXTRACTION,
        mode_default={
            CompileMode.FAST: False,
            CompileMode.STANDARD: True,
            CompileMode.DEEP: True,
        },
    )
    enable_table = _capability_enabled(
        plan, Capability.TABLE_EXTRACTION,
        mode_default={
            CompileMode.FAST: False,
            CompileMode.STANDARD: True,
            CompileMode.DEEP: True,
        },
    )
    enable_equation = _capability_enabled(
        plan, Capability.FORMULA_EXTRACTION,
        mode_default={
            CompileMode.FAST: False,
            CompileMode.STANDARD: False,  # opt-in even in standard
            CompileMode.DEEP: True,
        },
    )

    # ---- capability-not-supported handling ------------------------
    # RAGAnything's CONFIG layer exposes per-capability toggles
    # (enable_image_processing / enable_table_processing /
    # enable_equation_processing). The bridge applies the
    # `config_overrides` returned below to the `RAGAnythingConfig`
    # instance, so plan-driven switches DO reach the parser. The
    # only "unhandled" cases left are:
    #
    #   * The deployment explicitly disabled support via
    #     `J1_RAGANYTHING_SUPPORTS_*`. Operators use this when their
    #     vendor build doesn't have the capability wired (e.g. a
    #     pinned version where `enable_equation_processing` was
    #     removed).
    #   * OCR, which doesn't have a config-level toggle — it rides
    #     on `parse_method`. We already promoted DEEP+OCR plans to
    #     `parse_method="ocr"` above; what's left here is the case
    #     where the plan requires OCR but parse_method resolved to
    #     something else (e.g. allowed_parse_methods constrained it).
    unhandled: list[str] = []
    for required in plan.required_capabilities:
        if required in (
            Capability.TEXT_EXTRACTION, Capability.LAYOUT_DETECTION,
        ):
            continue  # always honoured
        if required == Capability.OCR and parse_method == "ocr":
            continue  # honoured
        if required == Capability.OCR and parse_method != "ocr":
            msg = (
                "plan requires OCR but parse_method resolved to "
                f"{parse_method!r}; RAGAnything's `auto` may still "
                "OCR scanned pages, but the contract isn't guaranteed"
            )
            if plan.fallback_policy == FallbackPolicy.FAIL:
                raise CompileCapabilityUnsupported(required, msg)
            warnings.append(msg)
            unhandled.append(required.value)
            continue
        if required == Capability.IMAGE_EXTRACTION and not _settings_supports(
            settings, "supports_image", default=True,
        ):
            msg = (
                f"plan requires {required.value} but the "
                "deployment marks image processing unsupported "
                "(J1_RAGANYTHING_SUPPORTS_IMAGE=false)"
            )
            if plan.fallback_policy == FallbackPolicy.FAIL:
                raise CompileCapabilityUnsupported(required, msg)
            warnings.append(msg)
            unhandled.append(required.value)
            continue
        if required == Capability.TABLE_EXTRACTION and not _settings_supports(
            settings, "supports_table", default=True,
        ):
            msg = (
                f"plan requires {required.value} but the "
                "deployment marks table processing unsupported"
            )
            if plan.fallback_policy == FallbackPolicy.FAIL:
                raise CompileCapabilityUnsupported(required, msg)
            warnings.append(msg)
            unhandled.append(required.value)
            continue
        if required == Capability.FORMULA_EXTRACTION and not _settings_supports(
            settings, "supports_equation", default=True,
        ):
            msg = (
                f"plan requires {required.value} but the "
                "deployment marks equation processing unsupported"
            )
            if plan.fallback_policy == FallbackPolicy.FAIL:
                raise CompileCapabilityUnsupported(required, msg)
            warnings.append(msg)
            unhandled.append(required.value)
            continue
        # IMAGE / TABLE / FORMULA without a deployment-level
        # `supports_*=false` flag: applied at the config layer via
        # `config_overrides` below. NOT recorded as unhandled.

    return CompileConfig(
        parse_method=parse_method,
        enable_image_processing=enable_image,
        enable_table_processing=enable_table,
        enable_equation_processing=enable_equation,
        warnings=tuple(warnings),
        unhandled_capabilities=tuple(unhandled),
        resolved_mode=plan.mode.value,
    )


def _capability_enabled(
    plan: AssessmentPlan,
    capability: Capability,
    *,
    mode_default: dict[CompileMode, bool],
) -> bool:
    """Capability-toggle resolution: required > optional > mode default."""
    if plan.requires(capability):
        return True
    if capability in plan.optional_capabilities:
        return True
    return mode_default.get(plan.mode, False)


def _allowed_parse_methods(settings: RAGAnythingSettings) -> frozenset[str]:
    """Deployment-defined parse-method allow-list. When unset (the
    common case), every method MinerU's CLI accepts is permitted."""
    raw = getattr(settings, "allowed_parse_methods", None)
    if raw is None or not raw:
        return frozenset(VALID_PARSE_METHODS)
    return frozenset(raw)


def _settings_supports(
    settings: RAGAnythingSettings,
    field_name: str,
    *,
    default: bool,
) -> bool:
    """Read an optional `supports_*` field from settings. The settings
    dataclass doesn't define these today; deployments that want to
    constrain capabilities can subclass `RAGAnythingSettings` or set
    the corresponding env vars (`J1_RAGANYTHING_SUPPORTS_IMAGE` etc.)
    once those are wired through `load_raganything_settings`."""
    return bool(getattr(settings, field_name, default))


__all__ = [
    "CompileCapabilityUnsupported",
    "CompileConfig",
    "map_assessment_to_raganything_config",
]
