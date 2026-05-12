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

ENV_LLM_PLANNING_ENABLED = "J1_LLM_PLANNING_ENABLED"
ENV_PLANNING_MODEL_PROFILE = "J1_PLANNING_MODEL_PROFILE"
ENV_PLANNING_MAX_SAMPLE_BLOCKS = "J1_PLANNING_MAX_SAMPLE_BLOCKS"
ENV_PLANNING_MAX_PREVIEW_CHARS = "J1_PLANNING_MAX_PREVIEW_CHARS"
# Operator-facing plan mode. Maps to the legacy `llm_planning_enabled`
# flag below; carried as a single env so docs and dashboards have one
# knob to point at.
#  rule_based → deterministic only (default; cheap + safe)
#  llm        → LLM-assisted; deterministic plan still ships as the
#                rule_based comparison block
#  hybrid     → both run; LLM result wins on agreement, rule-based is
#                the safety net on disagreement / failure
ENV_INGEST_PLAN_MODE = "J1_INGEST_PLAN_MODE"


PLAN_MODE_RULE_BASED = "rule_based"
PLAN_MODE_LLM = "llm"
PLAN_MODE_HYBRID = "hybrid"

ALLOWED_PLAN_MODES: frozenset[str] = frozenset({
    PLAN_MODE_RULE_BASED, PLAN_MODE_LLM, PLAN_MODE_HYBRID,
})


__all__ = [
    "ALLOWED_PLAN_MODES",
    "ENV_INGEST_PLAN_MODE",
    "ENV_LLM_PLANNING_ENABLED",
    "ENV_PLANNING_MAX_PREVIEW_CHARS",
    "ENV_PLANNING_MAX_SAMPLE_BLOCKS",
    "ENV_PLANNING_MODEL_PROFILE",
    "PLAN_MODE_HYBRID",
    "PLAN_MODE_LLM",
    "PLAN_MODE_RULE_BASED",
    "PlanningSettings",
    "load_planning_settings",
]


@dataclass(frozen=True)
class PlanningSettings:
    """Resolved Planning Report settings.

 `llm_planning_enabled` enables the optional LLM-assisted planning
 pass. Default OFF — rule-based planning is the documented baseline.

 `model_profile` names the registered FAST/PREMIUM LLM role to use
 when `llm_planning_enabled=True`. Free-form string so deployments
 can register their own profile names.

 `max_sample_blocks` and `max_preview_chars` cap the digest fed to
 the LLM planner. Both are privacy boundaries: the planner must
 NEVER see the full raw document."""

    llm_planning_enabled: bool = False
    model_profile: str = "fast_planner"
    max_sample_blocks: int = 20
    max_preview_chars: int = 300


def load_planning_settings(
    env: Mapping[str, str] | None = None,
) -> PlanningSettings:
    """Read every `J1_PLANNING_*` env var into typed settings.

 Always returns a `PlanningSettings`. Bad numeric values raise
 `ConfigError` so misconfiguration surfaces at startup rather than
 silently degrading at runtime.

 `J1_INGEST_PLAN_MODE` is the operator-facing knob; when set it
 overrides `J1_LLM_PLANNING_ENABLED`. The legacy env stays
 supported for deployments that haven't migrated. Resolution rule:
 * `J1_INGEST_PLAN_MODE=llm` → llm_planning_enabled=True
 * `J1_INGEST_PLAN_MODE=hybrid` → llm_planning_enabled=True
   (rule-based runs first; LLM augments — same code path)
 * `J1_INGEST_PLAN_MODE=rule_based` (default) → llm_planning_enabled=False
 * unset → fall through to `J1_LLM_PLANNING_ENABLED` legacy default
 """
    source = env if env is not None else os.environ
    plan_mode = _plan_mode(source)
    if plan_mode is not None:
        llm_enabled_default = plan_mode in (PLAN_MODE_LLM, PLAN_MODE_HYBRID)
    else:
        llm_enabled_default = False
    return PlanningSettings(
        llm_planning_enabled=_bool(
            source, ENV_LLM_PLANNING_ENABLED, default=llm_enabled_default,
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


def _plan_mode(env: Mapping[str, str]) -> str | None:
    """Resolve the operator-facing plan mode.

 Returns None when the env var is unset (caller falls back to
 `rule_based` + the legacy `J1_LLM_PLANNING_ENABLED` flag).
 Raises `ConfigError` on a recognisable-but-invalid value so a
 typo doesn't silently degrade to rule_based."""
    raw = env.get(ENV_INGEST_PLAN_MODE)
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if text not in ALLOWED_PLAN_MODES:
        raise ConfigError(
            f"{ENV_INGEST_PLAN_MODE}={raw!r} is not a recognised plan "
            f"mode (accepted: {sorted(ALLOWED_PLAN_MODES)})"
        )
    return text
