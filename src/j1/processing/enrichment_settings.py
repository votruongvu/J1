"""Wave 7 — enrichment LLM + concurrency settings.

Centralises the deployment-level knobs that gate enrichment-stage
LLM calls. Read once at worker bootstrap from environment
variables, then passed through to the limiter / enricher
constructors. Pure / no I/O at module import time.

Env-var vocabulary (all optional; defaults below are dev-safe):

  * `J1_ENRICHMENT_ENABLED`                       (default `true`)
  * `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS`      (default `1`)
  * `J1_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS` (default `1`)
  * `J1_ENRICHMENT_TIMEOUT_SECONDS`               (default `120`)
  * `J1_ENRICHMENT_RETRY_LIMIT`                   (default `1`)
  * `J1_ENRICHMENT_REQUIRE_SUCCESS`               (default `false`)
  * `J1_DEFAULT_ENRICHMENT_MODEL_TIER`            (default `fast`)
  * `J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS`  (default `true`)

Design rules:

  1. **Dev-safe defaults.** The defaults must NEVER overload a
     small laptop or shared CI runner. `MAX_CONCURRENT_LLM_CALLS=1`
     serialises every enricher's LLM call by default; ops who want
     parallelism opt in explicitly via the env var.

  2. **Separate from Temporal concurrency.** This module knows
     nothing about Temporal worker concurrency / activity slots.
     The limiter wraps the per-LLM-client call site; Temporal's
     own worker concurrency is configured separately.
     `max_concurrent_llm_calls` is specifically the EXTERNAL LLM
     call ceiling — the limit that bounds outbound HTTP requests
     to vendor APIs (OpenAI, local LM Studio, etc.). Temporal
     worker activity concurrency lives in worker config
     (`max_concurrent_activities`) and isn't affected by this
     setting.

  3. **One shared semaphore is intentional for now.** The Wave-7
     limiter wraps every LLM client (text + vision) with ONE
     shared semaphore so the worker-wide ceiling is the single
     knob operators tune. Per-tier semaphores (separate budgets
     for text vs. vision) are deferred — they're a real
     optimisation when one tier becomes a bottleneck, but
     premature today. The split can be added later by passing
     different `LLMCallLimiter` instances to different enricher
     subclasses at composition time.

  4. **Compile retry stays separate.** `J1_COMPILE_*` knobs live on
     `ProjectProcessingRequest.compile_*` fields and the
     `compile_retry` module — Wave 7 enrichment settings don't
     mention them. Two different retry budgets, two different
     failure modes.

  5. **Conservative-limits dev mode.** When
     `J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS=true` (default)
     the loader caps every concurrency knob at the dev-safe
     ceiling regardless of the operator-supplied value. Lets a
     deployment script set ambitious production values + the env
     stay safe for dev/test without manual stripping.

  6. **Skeleton modules don't need limiter wiring.** The Wave-6
     skeleton modules (`MetadataEnrichmentModule`,
     `TerminologyEnrichmentModule`, `ValidationEnrichmentModule`)
     don't call LLMs — they project compile signals + domain
     hints into typed records. The limiter is plumbed via
     `_LLMBackedEnricher.__init__` for the legacy enrichers in
     `j1.enrichers` that DO call LLMs. When the LLM-wiring slice
     ships, the new modules take the same `llm_call_limiter`
     kwarg.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


__all__ = [
    "DEV_SAFE_MAX_CONCURRENT_LLM_CALLS",
    "DEV_SAFE_MAX_CONCURRENT_TASKS",
    "DEV_SAFE_TIMEOUT_SECONDS",
    "DEV_SAFE_RETRY_LIMIT",
    "ENV_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS",
    "ENV_ENRICHMENT_ENABLED",
    "ENV_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS",
    "ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS",
    "ENV_ENRICHMENT_REQUIRE_SUCCESS",
    "ENV_ENRICHMENT_RETRY_LIMIT",
    "ENV_ENRICHMENT_TIMEOUT_SECONDS",
    "ENV_DEFAULT_ENRICHMENT_MODEL_TIER",
    "MODEL_TIER_FAST",
    "MODEL_TIER_PREMIUM",
    "MODEL_TIER_VISION",
    "EnrichmentConcurrencySettings",
    "load_enrichment_settings",
]


# ---- Env-var names (single source of truth) -----------------------

ENV_ENRICHMENT_ENABLED = "J1_ENRICHMENT_ENABLED"
ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS = "J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS"
ENV_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS = (
    "J1_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS"
)
ENV_ENRICHMENT_TIMEOUT_SECONDS = "J1_ENRICHMENT_TIMEOUT_SECONDS"
ENV_ENRICHMENT_RETRY_LIMIT = "J1_ENRICHMENT_RETRY_LIMIT"
ENV_ENRICHMENT_REQUIRE_SUCCESS = "J1_ENRICHMENT_REQUIRE_SUCCESS"
ENV_DEFAULT_ENRICHMENT_MODEL_TIER = "J1_DEFAULT_ENRICHMENT_MODEL_TIER"
ENV_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS = (
    "J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS"
)


# ---- Model tier vocabulary ---------------------------------------

MODEL_TIER_FAST = "fast"
MODEL_TIER_PREMIUM = "premium"
MODEL_TIER_VISION = "vision"

_VALID_MODEL_TIERS = frozenset({
    MODEL_TIER_FAST, MODEL_TIER_PREMIUM, MODEL_TIER_VISION,
})


# ---- Dev-safe ceiling values --------------------------------------
# Used both as the default-default AND as the cap when
# `J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS=true`. Pinned numbers,
# not env-overridable, on purpose — the safety net must be fixed.

DEV_SAFE_MAX_CONCURRENT_LLM_CALLS = 1
DEV_SAFE_MAX_CONCURRENT_TASKS = 1
DEV_SAFE_TIMEOUT_SECONDS = 120.0
DEV_SAFE_RETRY_LIMIT = 1


# ---- Settings dataclass -------------------------------------------


@dataclass(frozen=True)
class EnrichmentConcurrencySettings:
    """The resolved enrichment-stage settings for one worker.

    Pure data — built once at bootstrap from
    `load_enrichment_settings()` and passed through to the
    `LLMCallLimiter` + enricher constructors. Every field has a
    documented default so missing env vars produce a safe config."""

    enabled: bool = True
    # Per-LLM-call ceiling enforced by the limiter — caps the
    # number of in-flight `text_client.extract()` /
    # `vision_client.analyze_image()` calls across enrichers.
    max_concurrent_llm_calls: int = DEV_SAFE_MAX_CONCURRENT_LLM_CALLS
    # Per-document enrichment-task ceiling. Today's
    # `CompositeEnricher` runs sequentially so this is informational;
    # future parallel modules will respect it.
    max_concurrent_enrichment_tasks: int = DEV_SAFE_MAX_CONCURRENT_TASKS
    # Wall-clock timeout for a single LLM call. The limiter raises
    # `TimeoutError` after this many seconds — the enricher
    # converts it into a soft skip.
    timeout_seconds: float = DEV_SAFE_TIMEOUT_SECONDS
    # Bounded retries on transient failures. 0 disables retry; 1
    # means "try once, then retry once".
    retry_limit: int = DEV_SAFE_RETRY_LIMIT
    # Workflow-level enforcement flag — when True, a failed
    # enrichment stage fails the run with
    # `FAILURE_CODE_ENRICHMENT_REQUIRED`. Mirrors the per-domain
    # policy field; the env var is the deployment fallback when no
    # pack expresses an opinion.
    require_enrichment_success: bool = False
    # Default LLM tier for enrichment when neither the domain
    # policy nor the run override picks one. `vision` is gated
    # separately on whether image content exists.
    default_model_tier: str = MODEL_TIER_FAST
    # When True, the loader caps every concurrency value at the
    # `DEV_SAFE_*` ceiling regardless of what the env var says.
    # Default True so a missing-env deployment is automatically
    # dev-safe; production opt-out is explicit.
    dev_mode_conservative_limits: bool = True


# ---- Loader -------------------------------------------------------


def load_enrichment_settings(
    env: dict[str, str] | None = None,
) -> EnrichmentConcurrencySettings:
    """Build an `EnrichmentConcurrencySettings` from `env` (defaults
    to `os.environ`). Tolerant: malformed values fall back to
    documented defaults rather than crashing the worker."""
    source = env if env is not None else os.environ
    enabled = _read_bool(
        source, ENV_ENRICHMENT_ENABLED, default=True,
    )
    require_success = _read_bool(
        source, ENV_ENRICHMENT_REQUIRE_SUCCESS, default=False,
    )
    dev_mode = _read_bool(
        source, ENV_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS, default=True,
    )
    max_llm = _read_positive_int(
        source, ENV_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS,
        default=DEV_SAFE_MAX_CONCURRENT_LLM_CALLS,
    )
    max_tasks = _read_positive_int(
        source, ENV_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS,
        default=DEV_SAFE_MAX_CONCURRENT_TASKS,
    )
    timeout = _read_positive_float(
        source, ENV_ENRICHMENT_TIMEOUT_SECONDS,
        default=DEV_SAFE_TIMEOUT_SECONDS,
    )
    retry_limit = _read_non_negative_int(
        source, ENV_ENRICHMENT_RETRY_LIMIT,
        default=DEV_SAFE_RETRY_LIMIT,
    )
    model_tier = _read_model_tier(
        source, ENV_DEFAULT_ENRICHMENT_MODEL_TIER,
        default=MODEL_TIER_FAST,
    )

    # Dev-mode cap. Applied AFTER reading the raw values so an
    # operator who explicitly sets a high value still gets capped
    # in dev unless they ALSO disable the dev flag.
    if dev_mode:
        max_llm = min(max_llm, DEV_SAFE_MAX_CONCURRENT_LLM_CALLS)
        max_tasks = min(max_tasks, DEV_SAFE_MAX_CONCURRENT_TASKS)
        timeout = min(timeout, DEV_SAFE_TIMEOUT_SECONDS)
        retry_limit = min(retry_limit, DEV_SAFE_RETRY_LIMIT)

    return EnrichmentConcurrencySettings(
        enabled=enabled,
        max_concurrent_llm_calls=max_llm,
        max_concurrent_enrichment_tasks=max_tasks,
        timeout_seconds=timeout,
        retry_limit=retry_limit,
        require_enrichment_success=require_success,
        default_model_tier=model_tier,
        dev_mode_conservative_limits=dev_mode,
    )


# ---- Tiny env-parse helpers (kept private) -------------------------


def _read_bool(env, key: str, *, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _read_positive_int(env, key: str, *, default: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _read_non_negative_int(env, key: str, *, default: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _read_positive_float(env, key: str, *, default: float) -> float:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _read_model_tier(env, key: str, *, default: str) -> str:
    raw = env.get(key)
    if raw is None:
        return default
    candidate = str(raw).strip().lower()
    return candidate if candidate in _VALID_MODEL_TIERS else default
