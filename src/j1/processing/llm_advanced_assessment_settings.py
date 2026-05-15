"""Settings for the optional LLM-based Advanced Assessment.

Advanced Assessment is OFF by default and only runs when an
operator clicks "Run Advanced Assessment" on the picker. Even when
enabled, it refuses inputs that would be expensive or unsafe to
send to an LLM — see :class:`LLMAdvancedAssessmentSettings` for the
exact guardrails.

Loaded once at REST startup. Tests pass a ``Mapping`` directly so
the env can be overridden without touching ``os.environ``.

Env vocabulary:

  J1_LLM_ADVANCED_ASSESSMENT_ENABLED        bool  default false
  J1_LLM_ADVANCED_ASSESSMENT_MAX_FILE_SIZE  int   default 5_000_000 (5 MB)
  J1_LLM_ADVANCED_ASSESSMENT_MAX_PAGES      int   default 200
  J1_LLM_ADVANCED_ASSESSMENT_MAX_CHARS      int   default 60_000
  J1_LLM_ADVANCED_ASSESSMENT_MAX_SAMPLED_PAGES  int   default 6
  J1_LLM_ADVANCED_ASSESSMENT_TIMEOUT_SECONDS    int   default 60
  J1_LLM_ADVANCED_ASSESSMENT_ALLOW_FILE_UPLOAD  bool  default false

Refusal contract: when the document exceeds any limit the service
returns a structured refusal payload (not a 4xx). The FE renders the
operator-readable message and asks the user to pick manually.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


__all__ = [
    "LLMAdvancedAssessmentSettings",
    "load_llm_advanced_assessment_settings",
]


ENV_ENABLED = "J1_LLM_ADVANCED_ASSESSMENT_ENABLED"
ENV_MAX_FILE_SIZE = "J1_LLM_ADVANCED_ASSESSMENT_MAX_FILE_SIZE"
ENV_MAX_PAGES = "J1_LLM_ADVANCED_ASSESSMENT_MAX_PAGES"
ENV_MAX_CHARS = "J1_LLM_ADVANCED_ASSESSMENT_MAX_CHARS"
ENV_MAX_SAMPLED_PAGES = "J1_LLM_ADVANCED_ASSESSMENT_MAX_SAMPLED_PAGES"
ENV_TIMEOUT_SECONDS = "J1_LLM_ADVANCED_ASSESSMENT_TIMEOUT_SECONDS"
ENV_ALLOW_FILE_UPLOAD = "J1_LLM_ADVANCED_ASSESSMENT_ALLOW_FILE_UPLOAD"


@dataclass(frozen=True)
class LLMAdvancedAssessmentSettings:
    """Operator-tunable guardrails for the Advanced Assessment.

    ``enabled`` is the only knob that should ever flip a deployment
    on; the size / page / char limits give an extra layer of
    protection against runaway cost when a user inadvertently
    triggers Advanced on a huge file. ``allow_file_upload`` defaults
    to ``False`` because not every LLM provider supports file upload
    safely — the sampled-text path is the default contract.
    """

    enabled: bool = False
    max_file_size_bytes: int = 5_000_000
    max_page_count: int = 200
    max_text_chars: int = 60_000
    max_sampled_pages: int = 6
    timeout_seconds: int = 60
    allow_file_upload: bool = False


def load_llm_advanced_assessment_settings(
    env: Mapping[str, str] | None = None,
) -> LLMAdvancedAssessmentSettings:
    """Read settings from env vars. Falls back to safe defaults on
    missing / unparseable values — no exceptions raised at boot so
    a misconfigured limit can't take down the API process. Operators
    who want strict validation can drive the same builder from
    their own config pipeline."""
    src: Mapping[str, str] = env if env is not None else os.environ
    return LLMAdvancedAssessmentSettings(
        enabled=_parse_bool(src.get(ENV_ENABLED), default=False),
        max_file_size_bytes=_parse_int(
            src.get(ENV_MAX_FILE_SIZE), default=5_000_000,
        ),
        max_page_count=_parse_int(
            src.get(ENV_MAX_PAGES), default=200,
        ),
        max_text_chars=_parse_int(
            src.get(ENV_MAX_CHARS), default=60_000,
        ),
        max_sampled_pages=_parse_int(
            src.get(ENV_MAX_SAMPLED_PAGES), default=6,
        ),
        timeout_seconds=_parse_int(
            src.get(ENV_TIMEOUT_SECONDS), default=60,
        ),
        allow_file_upload=_parse_bool(
            src.get(ENV_ALLOW_FILE_UPLOAD), default=False,
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


def _parse_int(raw: str | None, *, default: int) -> int:
    if raw is None:
        return default
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
