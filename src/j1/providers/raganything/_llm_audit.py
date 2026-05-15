"""LLM-call audit wrapper for the RAGAnything bridge.

Every text-LLM / vision-LLM callable wired into RAGAnything goes
through this module so the operator can answer "did this run
actually fire an LLM call, and if so for what purpose?" without
parsing prompts or replaying the workflow.

Two events emitted per wrapped invocation:

  * `ingest.stage.llm_call_started`   — fired BEFORE the underlying
    callable runs. Carries provider/model/purpose/selected_profile.
  * `ingest.stage.llm_call_completed` — fired AFTER (success OR
    failure). Carries `duration_ms` + `success: bool` so an
    operator can grep for slow calls without an APM tool.

Sampling: a single env flag, `J1_LLM_CALL_AUDIT_ENABLED` (default
`false`), gates emission for the REAL-LLM path. The no-op
callable used by `minimum_queryable` is ALWAYS audited
regardless of this flag — that's the keystone honesty signal and
the volume is bounded by chunk count, not LLM token budget.

Why a single boolean (not a sample rate): production deployments
that need the data want it whole; deployments that don't want
the log volume want it off. A 10% sample rate would hide the
worst-case slow-call signal without saving meaningful log bytes
on a worker that already INFO-logs every stage transition. If
volume becomes a real problem later, the wrapper can grow a
sample-rate field without changing the call sites.

Field hygiene: NEVER log prompts, system prompts, history
messages, or response bodies. Prompt/response previews are
explicitly NOT carried by this module — the per-call surface is
purpose + provider + model + selected_profile + latency. The
no-op's debugging `on_call` hook in
[`_noop_llm.py`](./_noop_llm.py) still gets the truncated
prompt preview, but it stays inside the audit hook closure and
is never written to the JSON-encoded extra fields.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass


_log = logging.getLogger(__name__)


ENV_LLM_CALL_AUDIT_ENABLED = "J1_LLM_CALL_AUDIT_ENABLED"

# Stable purpose vocabulary. Dashboards filter on these strings;
# add new values, don't rename existing ones.
PURPOSE_ENTITY_EXTRACTION = "entity_extraction"
PURPOSE_ENTITY_EXTRACTION_NOOP = "entity_extraction_noop_minimum_queryable"
PURPOSE_VISION_ANALYSIS = "vision_analysis"


@dataclass(frozen=True)
class LLMAuditConfig:
    """Resolved deployment audit config.

    `enabled` controls real-LLM-path emission. The no-op path
    ignores `enabled` and always emits — see module docstring.
    """

    enabled: bool = False

    def should_audit_real_calls(self) -> bool:
        return self.enabled


def load_llm_audit_config(
    env: Mapping[str, str] | None = None,
) -> LLMAuditConfig:
    """Resolve `LLMAuditConfig` from env. Mirrors the convention
    used by other settings modules — pure function, accepts an
    env mapping for test isolation."""
    src: Mapping[str, str] = env if env is not None else os.environ
    return LLMAuditConfig(
        enabled=_parse_bool(src.get(ENV_LLM_CALL_AUDIT_ENABLED), default=False),
    )


def wrap_audited_async(
    inner: Callable[..., object],
    *,
    purpose: str,
    stage: str,
    provider: str,
    model: str | None,
    selected_profile: str | None,
    always_audit: bool = False,
    config: LLMAuditConfig | None = None,
) -> Callable[..., object]:
    """Wrap an async LLM callable with start/complete audit events.

    The returned coroutine has the same signature as `inner`. It
    forwards every argument unchanged and returns the inner
    result verbatim. Audit events are emitted via the bridge
    logger so they land in the worker's log aggregator alongside
    every other ingest event.

    `always_audit=True` short-circuits the env-flag gate — used by
    the no-op callable so its events fire even when the
    deployment hasn't enabled real-LLM auditing.
    """
    cfg = config if config is not None else load_llm_audit_config()

    async def _audited(*args, **kwargs):
        if not (always_audit or cfg.should_audit_real_calls()):
            return await inner(*args, **kwargs)
        started_extra = {
            "event": "ingest.stage.llm_call_started",
            "stage": stage,
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "selected_profile": selected_profile,
        }
        _log.info("ingest.stage.llm_call_started", extra=started_extra)
        start_ns = time.monotonic_ns()
        success = False
        try:
            result = await inner(*args, **kwargs)
            success = True
            return result
        finally:
            duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            _log.info(
                "ingest.stage.llm_call_completed",
                extra={
                    "event": "ingest.stage.llm_call_completed",
                    "stage": stage,
                    "purpose": purpose,
                    "provider": provider,
                    "model": model,
                    "selected_profile": selected_profile,
                    "duration_ms": int(duration_ms),
                    "success": success,
                },
            )

    # Preserve the no-op's `call_count` attribute if present so
    # tests + the existing `_noop_llm` audit hook still see it.
    if hasattr(inner, "call_count"):
        _audited.call_count = 0  # type: ignore[attr-defined]

        # Forward the inner's count via a thin proxy so the wrapper
        # carries the same field. Re-reading the inner's counter on
        # every access keeps the two in sync without extra state.
        def _proxy_call_count() -> int:
            return getattr(inner, "call_count", 0)

        _audited.get_call_count = _proxy_call_count  # type: ignore[attr-defined]

    return _audited


def emit_heavy_operation_detected(
    *,
    stage: str,
    operation: str,
    selected_profile: str | None,
    detail: str | None = None,
) -> None:
    """Fire a single `ingest.stage.heavy_operation_detected` audit
    line. Used for hot paths the operator may want to budget
    against — MinerU parse start, vision-LLM invocation, etc.

    Called from the call site that knows the operation has begun
    (NOT from a periodic ticker — that would smear the signal).
    Field shape is stable; `detail` is an operator-readable
    short string, never document content.

    Always emitted regardless of `J1_LLM_CALL_AUDIT_ENABLED`:
    heavy-operation detection is a coarse-grained per-stage
    signal (~one event per compile per modality), not the
    per-call firehose. Volume is bounded.
    """
    extra: dict[str, object] = {
        "event": "ingest.stage.heavy_operation_detected",
        "stage": stage,
        "operation": operation,
        "selected_profile": selected_profile,
    }
    if detail is not None:
        extra["detail"] = detail
    _log.info("ingest.stage.heavy_operation_detected", extra=extra)


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    """Mirror the convention used in
    [`enrich_assessment_settings`](../../processing/enrich_assessment_settings.py)
    and [`execution_profile_policy`](../../processing/execution_profile_policy.py)."""
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
