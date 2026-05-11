"""Compile-safety retry: settings + per-attempt audit record + the
mode-escalation ladder.

This module is deliberately thin. The retry orchestration itself
lives in the workflow (`project_processing.py:_run_per_document`)
because that's where the compile activity is dispatched, the
`AssessmentPlan` is in scope, and Temporal's retry semantics are
honoured. Centralising the data shapes + escalation rules here
keeps the workflow code focused on flow control.

Design constraints (from the spec):

 * Retry only on CLEAR quality failures — the
 `CompileQualityEvaluator` decides what counts.
 * Bounded attempt count — `max_compile_attempts` defaults to 2
 (one retry). `deep` mode never retries beyond itself.
 * Idempotent — every attempt carries an `attempt_number` and the
 workflow uses the existing per-(document, processor_kind,
 version, mode) cache to avoid double-writing artifacts when
 Temporal retries a single attempt mid-flight. The cache key
 DOES include the attempt mode, so escalating from `fast` to
 `standard` is treated as a fresh cache entry, not a duplicate.
 * Vendor-neutral — `CompileAttemptRecord` mirrors the
 `AssessmentPlan` shape's vocabulary (`mode`, `parse_method`)
 rather than RAGAnything-specific names.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from j1.processing.assessment import CompileMode

# ---- Settings ----------------------------------------------------

ENV_RETRY_ENABLED = "J1_COMPILE_RETRY_ENABLED"
ENV_MAX_ATTEMPTS = "J1_COMPILE_MAX_ATTEMPTS"
ENV_MIN_TEXT_CHARS = "J1_COMPILE_RETRY_MIN_TEXT_CHARS"
ENV_MIN_CHUNKS = "J1_COMPILE_RETRY_MIN_CHUNKS"

DEFAULT_RETRY_ENABLED = True
DEFAULT_MAX_ATTEMPTS = 2  # initial + one retry; deep never escalates
DEFAULT_MIN_TEXT_CHARS = 200
DEFAULT_MIN_CHUNKS = 1


@dataclass(frozen=True)
class CompileRetrySettings:
    """Operator-tunable retry knobs. Plain dataclass so the workflow
 can pass it across the Temporal data converter without dragging
 in env-reading code from this module.

 Read once at REST/dev-wiring boundary via
 `load_compile_retry_settings` and threaded through
 `ProjectProcessingRequest.compile_retry_*` fields. The workflow
 reconstructs the dataclass from those fields for evaluator calls.
 """

    enabled: bool = DEFAULT_RETRY_ENABLED
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    min_text_chars: int = DEFAULT_MIN_TEXT_CHARS
    min_chunks: int = DEFAULT_MIN_CHUNKS


def load_compile_retry_settings(
    env: Mapping[str, str] | None = None,
) -> CompileRetrySettings:
    """Read retry settings from the env. Unknown / unparseable values
 quietly fall back to defaults — retry is a safety net, not a
 correctness gate."""
    source = env if env is not None else os.environ
    return CompileRetrySettings(
        enabled=_parse_bool(
            source.get(ENV_RETRY_ENABLED), default=DEFAULT_RETRY_ENABLED,
        ),
        max_attempts=_parse_int(
            source.get(ENV_MAX_ATTEMPTS), default=DEFAULT_MAX_ATTEMPTS,
            minimum=1,
        ),
        min_text_chars=_parse_int(
            source.get(ENV_MIN_TEXT_CHARS),
            default=DEFAULT_MIN_TEXT_CHARS, minimum=0,
        ),
        min_chunks=_parse_int(
            source.get(ENV_MIN_CHUNKS), default=DEFAULT_MIN_CHUNKS,
            minimum=0,
        ),
    )


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    return default


def _parse_int(raw: str | None, *, default: int, minimum: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(value, minimum)


# ---- Mode-escalation ladder --------------------------------------


_RETRY_LADDER: dict[CompileMode, CompileMode | None] = {
    # Two-mode model: STANDARD → DEEP → STOP. The planner never
    # emits FAST any more, but if a legacy plan (or a manually-
    # constructed test fixture) starts at FAST we escalate it
    # straight to STANDARD on the first retry so the document
    # still moves forward.
    CompileMode.FAST: CompileMode.STANDARD,
    CompileMode.STANDARD: CompileMode.DEEP,
    CompileMode.DEEP: None,
}


def next_compile_mode(current: CompileMode) -> CompileMode | None:
    """Return the mode the retry layer escalates to, or `None` when
 the document is already at the highest mode (`deep`). The
 workflow uses this as the only source of truth for "what's
 next" — operators tuning the ladder change it here.

 Two-mode model: the ladder is STANDARD → DEEP → STOP. The
 legacy FAST entry is preserved as a safety net (escalates
 straight to STANDARD) so legacy plans replayed from history
 still progress."""
    return _RETRY_LADDER.get(current)


# ---- Attempt record ---------------------------------------------


@dataclass(frozen=True)
class CompileAttemptRecord:
    """One audit entry for one compile attempt. Persisted on
 `ArtifactProcessingResult.metadata["compile_attempts"]` as a
 list of dicts (via `to_payload`) so the audit log + FE
 timeline see a stable shape.

 Vendor-neutral by design: `mode` is `CompileMode.value`,
 `parse_method` is the resolved string (`txt` / `auto` / `ocr`).
 Adapter-specific fields live on the per-attempt
 `mapped_compile_config` dict — that's where RAGAnything's
 `enable_*` toggles surface."""

    attempt_number: int
    mode: str  # CompileMode.value
    parser: str  # adapter name, e.g. "raganything"
    parse_method: str | None
    started_at: str  # ISO-8601 UTC
    completed_at: str | None
    status: str  # "succeeded" | "failed" | "retried"
    chunks_count: int
    extracted_text_chars: int | None
    quality: str  # QUALITY_GOOD | QUALITY_LOW | QUALITY_FAILED
    retry_reason: str | None
    warnings: tuple[str, ...] = ()
    mapped_compile_config: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "attempt_number": self.attempt_number,
            "mode": self.mode,
            "parser": self.parser,
            "parse_method": self.parse_method,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "chunks_count": self.chunks_count,
            "extracted_text_chars": self.extracted_text_chars,
            "quality": self.quality,
            "retry_reason": self.retry_reason,
            "warnings": list(self.warnings),
            "mapped_compile_config": dict(self.mapped_compile_config),
        }


__all__ = [
    "CompileAttemptRecord",
    "CompileRetrySettings",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MIN_CHUNKS",
    "DEFAULT_MIN_TEXT_CHARS",
    "DEFAULT_RETRY_ENABLED",
    "ENV_MAX_ATTEMPTS",
    "ENV_MIN_CHUNKS",
    "ENV_MIN_TEXT_CHARS",
    "ENV_RETRY_ENABLED",
    "load_compile_retry_settings",
    "next_compile_mode",
]
