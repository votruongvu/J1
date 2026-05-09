"""Planning Report stage configuration.

The Planning Report stage runs immediately after compile + content
inventory. It produces a `planning_result` projection that surfaces
WHAT the pipeline intends to do for a document — the modes / steps /
estimated cost / risk-level the existing `IngestPlan` already carries,
plus a lightweight content digest derived from the parsed-content
manifest.

This module owns only the env-var loader for that surface. The
projector itself lives in `j1.ingestion_review.service` so it can
compose audit-log + manifest reads against existing scaffolding.

Privacy: when LLM-assisted planning is enabled, the planning prompt
takes a SAMPLED digest of the parsed content (configurable cap) — it
must NOT receive the full raw document. The two cap envs
(`MAX_SAMPLE_BLOCKS`, `MAX_PREVIEW_CHARS`) are the privacy boundary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from j1.errors.exceptions import ConfigError

# ---- Env var names -----------------------------------------------------

ENV_PLANNING_ENABLED = "J1_PLANNING_ENABLED"
ENV_POST_COMPILE_PLANNING_ENABLED = "J1_POST_COMPILE_PLANNING_ENABLED"
ENV_LLM_PLANNING_ENABLED = "J1_LLM_PLANNING_ENABLED"
ENV_PLANNING_MODEL_PROFILE = "J1_PLANNING_MODEL_PROFILE"
ENV_PLANNING_MAX_SAMPLE_BLOCKS = "J1_PLANNING_MAX_SAMPLE_BLOCKS"
ENV_PLANNING_MAX_PREVIEW_CHARS = "J1_PLANNING_MAX_PREVIEW_CHARS"
ENV_PLANNING_MAX_EARLY_PAGES = "J1_PLANNING_MAX_EARLY_PAGES"
ENV_PLANNING_FAIL_OPEN = "J1_PLANNING_FAIL_OPEN"
ENV_PLANNING_TRACE_ENABLED = "J1_PLANNING_TRACE_ENABLED"
ENV_PLANNING_TRACE_BODY = "J1_PLANNING_TRACE_BODY"

# ---- Domain pack envs ------------------------------------------------

ENV_DOMAIN_PACKS_ENABLED = "J1_DOMAIN_PACKS_ENABLED"
ENV_DEFAULT_DOMAIN = "J1_DEFAULT_DOMAIN"
ENV_DOMAIN_DETECTION_ENABLED = "J1_DOMAIN_DETECTION_ENABLED"
ENV_DOMAIN_DETECTION_MIN_CONFIDENCE = "J1_DOMAIN_DETECTION_MIN_CONFIDENCE"
ENV_ALLOWED_DOMAIN_OVERRIDES = "J1_ALLOWED_DOMAIN_OVERRIDES"
ENV_WORKSPACE_DEFAULT_DOMAIN = "J1_WORKSPACE_DEFAULT_DOMAIN"


__all__ = [
    "ENV_ALLOWED_DOMAIN_OVERRIDES",
    "ENV_DEFAULT_DOMAIN",
    "ENV_DOMAIN_DETECTION_ENABLED",
    "ENV_DOMAIN_DETECTION_MIN_CONFIDENCE",
    "ENV_DOMAIN_PACKS_ENABLED",
    "ENV_LLM_PLANNING_ENABLED",
    "ENV_PLANNING_ENABLED",
    "ENV_PLANNING_FAIL_OPEN",
    "ENV_PLANNING_MAX_EARLY_PAGES",
    "ENV_PLANNING_MAX_PREVIEW_CHARS",
    "ENV_PLANNING_MAX_SAMPLE_BLOCKS",
    "ENV_PLANNING_MODEL_PROFILE",
    "ENV_PLANNING_TRACE_BODY",
    "ENV_PLANNING_TRACE_ENABLED",
    "ENV_POST_COMPILE_PLANNING_ENABLED",
    "ENV_WORKSPACE_DEFAULT_DOMAIN",
    "PlanningSettings",
    "load_planning_settings",
]


@dataclass(frozen=True)
class PlanningSettings:
    """Resolved Planning Report settings.

    `enabled` controls whether the projection is produced at all. The
    audit-log `plan.generated` event still drives the workflow; this
    flag only affects whether the FE Planning Report tab is populated.

    `llm_planning_enabled` enables the optional LLM-assisted planning
    pass. Default OFF — rule-based planning (the existing
    `DefaultIngestPlanner`) is the documented baseline.

    `model_profile` names the registered FAST/PREMIUM LLM role to use
    when `llm_planning_enabled=True`. Free-form string so deployments
    can register their own profile names.

    `max_sample_blocks` and `max_preview_chars` cap the digest fed to
    the LLM planner. Both are privacy boundaries: the planner must
    NEVER see the full raw document.

    `fail_open=True` (default) means: if the LLM-assisted planning path
    fails (timeout, parse error, etc.), the rule-based decision still
    stands and the run continues. `fail_open=False` would surface the
    failure as a planning warning — reserved for deployments that
    treat planning as a hard gate."""

    enabled: bool = True
    # Master switch for the post-compile planning stage. The Planning
    # Report tab can be enabled (`enabled=True`) without producing a
    # post-compile artifact when this flag is false — useful for
    # operators who only want the FE projection over the existing
    # `plan.generated` audit entries.
    post_compile_enabled: bool = True
    llm_planning_enabled: bool = False
    model_profile: str = "fast_planner"
    max_sample_blocks: int = 20
    max_preview_chars: int = 300
    max_early_pages: int = 3
    fail_open: bool = True
    # Diagnostic toggles. Default off — production deployments
    # should keep both flipped to avoid leaking planning context
    # bodies into operator logs. `trace_body` requires
    # `trace_enabled=True` to take effect.
    trace_enabled: bool = False
    trace_body: bool = False
    # ---- Domain pack settings -----------------------------------
    # Master switch for the domain-pack subsystem. Off → planner
    # behaves as if no packs were registered (always selects
    # `general`). The Planning Report still surfaces the empty
    # domain_context block so the wire shape stays stable.
    domain_packs_enabled: bool = True
    # Default domain when no override / detection signal applies.
    default_domain: str = "general"
    # Auto-detection switch. Off → only operator overrides can
    # select a non-generic domain.
    domain_detection_enabled: bool = True
    # Confidence floor for auto-detection. 0.65 matches the spec.
    domain_detection_min_confidence: float = 0.65
    # Allowlist of domain ids operators can request via override.
    # Comma-separated env value; defaults cover what's bundled.
    allowed_domain_overrides: tuple[str, ...] = ("general", "civil_engineering")
    # Workspace / project default domain. Falls below user override
    # but above auto-detection.
    workspace_default_domain: str = "general"


def load_planning_settings(
    env: Mapping[str, str] | None = None,
) -> PlanningSettings:
    """Read every `J1_PLANNING_*` env var into typed settings.

    Always returns a `PlanningSettings`. Bad numeric values raise
    `ConfigError` so misconfiguration surfaces at startup rather than
    silently degrading at runtime."""
    source = env if env is not None else os.environ
    return PlanningSettings(
        enabled=_bool(source, ENV_PLANNING_ENABLED, default=True),
        post_compile_enabled=_bool(
            source, ENV_POST_COMPILE_PLANNING_ENABLED, default=True,
        ),
        llm_planning_enabled=_bool(
            source, ENV_LLM_PLANNING_ENABLED, default=False,
        ),
        model_profile=(
            source.get(ENV_PLANNING_MODEL_PROFILE, "").strip()
            or "fast_planner"
        ),
        max_sample_blocks=_positive_int(
            source, ENV_PLANNING_MAX_SAMPLE_BLOCKS, default=20,
        ),
        max_preview_chars=_positive_int(
            source, ENV_PLANNING_MAX_PREVIEW_CHARS, default=300,
        ),
        max_early_pages=_positive_int(
            source, ENV_PLANNING_MAX_EARLY_PAGES, default=3,
        ),
        fail_open=_bool(source, ENV_PLANNING_FAIL_OPEN, default=True),
        trace_enabled=_bool(
            source, ENV_PLANNING_TRACE_ENABLED, default=False,
        ),
        trace_body=_bool(source, ENV_PLANNING_TRACE_BODY, default=False),
        domain_packs_enabled=_bool(
            source, ENV_DOMAIN_PACKS_ENABLED, default=True,
        ),
        default_domain=(
            source.get(ENV_DEFAULT_DOMAIN, "").strip().lower()
            or "general"
        ),
        domain_detection_enabled=_bool(
            source, ENV_DOMAIN_DETECTION_ENABLED, default=True,
        ),
        domain_detection_min_confidence=_float_in_unit_interval(
            source, ENV_DOMAIN_DETECTION_MIN_CONFIDENCE, default=0.65,
        ),
        allowed_domain_overrides=_csv(
            source, ENV_ALLOWED_DOMAIN_OVERRIDES,
            default=("general", "civil_engineering"),
        ),
        workspace_default_domain=(
            source.get(ENV_WORKSPACE_DEFAULT_DOMAIN, "").strip().lower()
            or "general"
        ),
    )


# ---- Parsing helpers ---------------------------------------------------


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _bool(env: Mapping[str, str], key: str, *, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"{key}={raw!r} is not a recognised boolean "
        f"(accepted: {sorted(_TRUE_VALUES | _FALSE_VALUES)})"
    )


def _positive_int(env: Mapping[str, str], key: str, *, default: int) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be > 0, got {value}")
    return value


def _float_in_unit_interval(
    env: Mapping[str, str], key: str, *, default: float,
) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if not (0.0 <= value <= 1.0):
        raise ConfigError(f"{key} must be in [0.0, 1.0], got {value}")
    return value


def _csv(
    env: Mapping[str, str], key: str, *, default: tuple[str, ...],
) -> tuple[str, ...]:
    """Parse a comma-separated list, lower-casing + stripping each
    entry. Empty value falls back to the default."""
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    parts = tuple(
        p.strip().lower() for p in raw.split(",") if p.strip()
    )
    return parts or default
