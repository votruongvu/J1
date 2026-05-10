"""Settings for the optional post-compile fast-LLM consult.

The fast-LLM consult is OFF by default. When enabled, it refines
ambiguous (OPTIONAL) rule-based enrich-assessment verdicts. The
consult MUST never:

  * be called before compile,
  * use a premium / expensive model by default,
  * fail ingestion (any failure → fall back to rule-based),
  * overrule deterministic SKIP decisions.

Config vocabulary (env-driven):

  * `J1_ENRICH_ASSESSMENT_FAST_LLM_ENABLED`            (default `false`)
  * `J1_ENRICH_ASSESSMENT_FAST_LLM_PROVIDER`           (e.g. `openai`)
  * `J1_ENRICH_ASSESSMENT_FAST_LLM_MODEL`              (e.g. `gpt-4o-mini`)
  * `J1_ENRICH_ASSESSMENT_FAST_LLM_TIMEOUT_SECONDS`    (default `10.0`)

The activity is wired at worker-bootstrap time with a callable that
talks to the configured LLM. Settings + callable resolution
happens at activity-time (NOT in workflow code) so the workflow
stays Temporal-sandbox-safe."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


ENV_FAST_LLM_ENABLED = "J1_ENRICH_ASSESSMENT_FAST_LLM_ENABLED"
ENV_FAST_LLM_PROVIDER = "J1_ENRICH_ASSESSMENT_FAST_LLM_PROVIDER"
ENV_FAST_LLM_MODEL = "J1_ENRICH_ASSESSMENT_FAST_LLM_MODEL"
ENV_FAST_LLM_TIMEOUT_SECONDS = "J1_ENRICH_ASSESSMENT_FAST_LLM_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class FastLLMConsultSettings:
    """Resolved settings for the optional fast-LLM consult.

    `is_actionable` is the gate the activity checks before doing any
    LLM work. When False (disabled, missing provider, missing model),
    the activity returns `consulted=False` and the workflow falls
    back to the rule-based plan."""

    enabled: bool = False
    provider: str | None = None
    model: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def is_actionable(self) -> bool:
        return bool(self.enabled and self.provider and self.model)


def load_fast_llm_consult_settings(
    env: Mapping[str, str] | None = None,
) -> FastLLMConsultSettings:
    """Resolve `FastLLMConsultSettings` from env vars. Defaults are
    safe (consult disabled, no provider, no model). Invalid timeout
    values silently fall back to the default rather than raising —
    a misconfigured timeout MUST NOT block ingestion."""
    src: Mapping[str, str] = env if env is not None else os.environ
    return FastLLMConsultSettings(
        enabled=_parse_bool(src.get(ENV_FAST_LLM_ENABLED), default=False),
        provider=_clean(src.get(ENV_FAST_LLM_PROVIDER)),
        model=_clean(src.get(ENV_FAST_LLM_MODEL)),
        timeout_seconds=_parse_float_seconds(
            src.get(ENV_FAST_LLM_TIMEOUT_SECONDS),
            default=DEFAULT_TIMEOUT_SECONDS,
        ),
    )


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    return default


def _clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _parse_float_seconds(raw: str | None, *, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return value
