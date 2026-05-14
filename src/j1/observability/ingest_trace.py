"""Dedicated ingestion performance trace.

This is a developer / operator debugging surface — disabled by default,
opt-in via ``J1_INGEST_TRACE_ENABLED=true``. It is **separate** from
``j1.audit`` (business events) and ``j1.logging`` (process-wide stderr).

When enabled, every traced stage emits one JSONL line per event to a
dedicated file (default ``logs/ingest_trace.jsonl``). The lines carry
correlation ids (``run_id``, ``document_id``, ``target_snapshot_id``,
``workflow_id``), duration, a ``slow`` flag, and a compact safe metadata
summary. Crucially the trace never logs document text, chunks,
embeddings, prompts, responses, or binary content — even when callers
hand it those fields, the writer strips them.

Two usage shapes:

  * ``trace_event(...)`` — one-shot event (e.g. "run created").
  * ``with trace_stage(...) as ts:`` — paired ``started`` / ``completed``
    or ``failed`` events with ``duration_ms``.

When trace is disabled both calls are fast no-ops. Callers should pass
``metadata`` builders lazily so the safe-summary work doesn't run on
the disabled path:

    with trace_stage(
        trace_event_base="ingest.compile",
        stage="compile",
        context=ctx,
        metadata_builder=lambda: {
            "parser": parser_kind,
            "compile_mode": compile_mode,
        },
    ):
        run_compile(...)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from j1.errors.exceptions import ConfigError


# ---- Env var names ----------------------------------------------------

ENV_INGEST_TRACE_ENABLED = "J1_INGEST_TRACE_ENABLED"
ENV_INGEST_TRACE_LEVEL = "J1_INGEST_TRACE_LEVEL"
ENV_INGEST_TRACE_SLOW_STAGE_MS = "J1_INGEST_TRACE_SLOW_STAGE_MS"
ENV_INGEST_TRACE_OUTPUT = "J1_INGEST_TRACE_OUTPUT"

_ALLOWED_LEVELS = frozenset({"INFO", "DEBUG"})
_DEFAULT_OUTPUT = "logs/ingest_trace.jsonl"
_DEFAULT_SLOW_STAGE_MS = 30_000

# Metadata keys we refuse to write to the trace file. Production code
# should never hand these to the trace helper; this is a defence in
# depth so a typo / refactor can't leak document content.
_UNSAFE_METADATA_KEYS: frozenset[str] = frozenset({
    "text",
    "content",
    "chunks",
    "embedding",
    "embeddings",
    "prompt",
    "prompts",
    "response",
    "responses",
    "ocr_output",
    "image_bytes",
    "raw_bytes",
})

# Hard cap on string values inside ``metadata``. Operators occasionally
# want short error strings or file paths; anything past this is a
# payload, not a summary.
_METADATA_VALUE_TRUNCATE_CHARS = 240

# Hard cap on ``error_message`` so stack traces don't bloat the file.
_ERROR_MESSAGE_TRUNCATE_CHARS = 300


__all__ = [
    "ENV_INGEST_TRACE_ENABLED",
    "ENV_INGEST_TRACE_LEVEL",
    "ENV_INGEST_TRACE_OUTPUT",
    "ENV_INGEST_TRACE_SLOW_STAGE_MS",
    "IngestTraceLogger",
    "IngestTraceSettings",
    "TraceContext",
    "current_ingest_trace_logger",
    "load_ingest_trace_settings",
    "reset_ingest_trace_logger",
    "trace_event",
    "trace_stage",
]


# ---- Settings ---------------------------------------------------------


@dataclass(frozen=True)
class IngestTraceSettings:
    """Resolved settings for the ingestion trace surface.

    ``enabled=False`` is the default; production deployments leave the
    trace off and turn it on only when investigating slowness, retries,
    or run-isolation suspicions.
    """

    enabled: bool = False
    level: str = "INFO"
    slow_stage_ms: int = _DEFAULT_SLOW_STAGE_MS
    output_path: str = _DEFAULT_OUTPUT


def load_ingest_trace_settings(
    env: Mapping[str, str] | None = None,
) -> IngestTraceSettings:
    """Read every ``J1_INGEST_TRACE_*`` env var into typed settings.

    Bad values raise ``ConfigError`` so misconfiguration surfaces at
    startup rather than silently degrading at runtime.
    """
    source = env if env is not None else os.environ
    enabled = _bool(source, ENV_INGEST_TRACE_ENABLED, default=False)
    level_raw = (source.get(ENV_INGEST_TRACE_LEVEL) or "").strip().upper()
    level = level_raw or "INFO"
    if level not in _ALLOWED_LEVELS:
        raise ConfigError(
            f"{ENV_INGEST_TRACE_LEVEL}={level_raw!r} is not a recognised "
            f"level (accepted: {sorted(_ALLOWED_LEVELS)})"
        )
    slow_ms = _positive_int(
        source, ENV_INGEST_TRACE_SLOW_STAGE_MS,
        default=_DEFAULT_SLOW_STAGE_MS,
    )
    output = (
        source.get(ENV_INGEST_TRACE_OUTPUT, "").strip()
        or _DEFAULT_OUTPUT
    )
    return IngestTraceSettings(
        enabled=enabled,
        level=level,
        slow_stage_ms=slow_ms,
        output_path=output,
    )


# ---- Correlation context ---------------------------------------------


@dataclass(frozen=True)
class TraceContext:
    """Identity carried with every trace event.

    Every field is optional because trace points fire at different
    boundaries (REST request reception has no ``run_id`` yet; activity
    code has all of them; workflow code rarely has ``activity``).
    ``run_id`` is the canonical correlation key — emit it whenever
    available.
    """

    tenant_id: str | None = None
    project_id: str | None = None
    document_id: str | None = None
    run_id: str | None = None
    target_snapshot_id: str | None = None
    snapshot_id: str | None = None
    workflow_id: str | None = None
    activity: str | None = None
    attempt: int | None = None


# ---- Logger ----------------------------------------------------------


class IngestTraceLogger:
    """Writes ingestion trace events to a dedicated JSONL file.

    Construct with explicit settings in tests; production code goes
    through :func:`current_ingest_trace_logger` which lazily reads the
    environment.
    """

    def __init__(self, settings: IngestTraceSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        # Cache the boolean so the hot disabled path is one attribute
        # lookup, not a settings + env round-trip.
        self._enabled: bool = bool(settings.enabled)
        self._slow_ms: int = int(settings.slow_stage_ms)
        self._output_path = Path(settings.output_path)
        # Cache a logger handle for slow-stage warnings (different
        # logger from the JSONL writer so operators can filter slow
        # warnings out of the normal log via the usual handlers).
        self._warn_log = logging.getLogger("j1.ingest_trace.slow_stage")
        # Bypass parents (root has its own handlers in dev wiring);
        # the JSONL file is the canonical trace sink.
        # Mark a partial-write recovery flag so a once-failed write
        # doesn't keep spamming warnings on every event.
        self._write_failure_warned = False

    # ---- Public interface --------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def settings(self) -> IngestTraceSettings:
        return self._settings

    @property
    def slow_stage_ms(self) -> int:
        return self._slow_ms

    def emit(
        self,
        *,
        trace_event: str,
        stage: str,
        status: str,
        context: TraceContext | None = None,
        duration_ms: int | None = None,
        slow: bool | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Write one event to the JSONL file. No-op when disabled."""
        if not self._enabled:
            return
        record = self._build_record(
            trace_event=trace_event,
            stage=stage,
            status=status,
            context=context,
            duration_ms=duration_ms,
            slow=slow,
            error_type=error_type,
            error_message=error_message,
            metadata=metadata,
        )
        self._write_record(record)

    # ---- Internals ---------------------------------------------------

    def _build_record(
        self,
        *,
        trace_event: str,
        stage: str,
        status: str,
        context: TraceContext | None,
        duration_ms: int | None,
        slow: bool | None,
        error_type: str | None,
        error_message: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": _utc_iso_now(),
            "trace_event": trace_event,
            "stage": stage,
            "status": status,
        }
        if context is not None:
            for field_name in (
                "tenant_id", "project_id", "document_id", "run_id",
                "target_snapshot_id", "snapshot_id", "workflow_id",
                "activity",
            ):
                value = getattr(context, field_name)
                if value is not None:
                    record[field_name] = value
            if context.attempt is not None:
                record["attempt"] = context.attempt
        if duration_ms is not None:
            record["duration_ms"] = int(duration_ms)
        if slow is not None:
            record["slow"] = bool(slow)
        if error_type is not None:
            record["error_type"] = str(error_type)
        if error_message is not None:
            record["error_message"] = _truncate(
                error_message, _ERROR_MESSAGE_TRUNCATE_CHARS,
            )
        if metadata:
            sanitized = _sanitize_metadata(metadata)
            if sanitized:
                record["metadata"] = sanitized
        return record

    def _write_record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self._output_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            # A successful write clears the "we've already warned"
            # latch so a later failure produces one fresh warning.
            self._write_failure_warned = False
        except OSError as exc:
            if not self._write_failure_warned:
                self._warn_log.warning(
                    "ingest.trace.write_failed path=%s error=%s",
                    self._output_path, exc,
                )
                self._write_failure_warned = True

    def emit_slow_warning(
        self,
        *,
        trace_event: str,
        stage: str,
        duration_ms: int,
        context: TraceContext | None,
    ) -> None:
        """One structured warning on the normal logger for every slow
        stage. Operators can grep ``ingest.trace.slow_stage`` to find
        all of them without trawling the JSONL file.
        """
        extra: dict[str, Any] = {
            "stage": stage,
            "trace_event": trace_event,
            "duration_ms": int(duration_ms),
            "threshold_ms": self._slow_ms,
        }
        if context is not None:
            for f in ("run_id", "document_id", "project_id", "workflow_id"):
                v = getattr(context, f, None)
                if v is not None:
                    extra[f] = v
        self._warn_log.warning("ingest.trace.slow_stage", extra=extra)


# ---- Module-level convenience -----------------------------------------
#
# Production callsites use the singleton accessed via
# :func:`current_ingest_trace_logger`; tests can :func:`reset_ingest_
# trace_logger` between cases to re-read env vars or to inject a
# pre-built instance.

_INSTANCE: IngestTraceLogger | None = None
_INSTANCE_LOCK = threading.Lock()


def current_ingest_trace_logger() -> IngestTraceLogger:
    """Return the process-wide trace logger, building it on first use.

    Re-reads ``J1_INGEST_TRACE_*`` env vars; cached after the first
    call so production callsites pay the env-read cost once.
    """
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = IngestTraceLogger(load_ingest_trace_settings())
        return _INSTANCE


def reset_ingest_trace_logger(
    instance: IngestTraceLogger | None = None,
) -> None:
    """Test hook: drop the cached singleton (or replace it with the
    supplied instance). Production code should not call this."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = instance


def trace_event(
    *,
    trace_event: str,
    stage: str,
    status: str,
    context: TraceContext | None = None,
    metadata: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
    duration_ms: int | None = None,
    slow: bool | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    """Emit a single trace event. No-op when trace is disabled."""
    logger = current_ingest_trace_logger()
    if not logger.enabled:
        return
    meta = _resolve_metadata(metadata)
    logger.emit(
        trace_event=trace_event,
        stage=stage,
        status=status,
        context=context,
        duration_ms=duration_ms,
        slow=slow,
        error_type=error_type,
        error_message=error_message,
        metadata=meta,
    )


@contextmanager
def trace_stage(
    *,
    trace_event_base: str,
    stage: str,
    context: TraceContext | None = None,
    metadata_builder: Callable[[], Mapping[str, Any]] | None = None,
) -> Iterator["StageHandle"]:
    """Time a stage and emit ``started`` + ``completed`` (or
    ``failed``) events.

    Yields a :class:`StageHandle` so callers can attach extra metadata
    discovered mid-stage (e.g. the artifact count produced by a
    compile). The metadata builder still runs at exit when present so
    one-shot callers don't have to thread the handle through deep call
    stacks.
    """
    logger = current_ingest_trace_logger()
    handle = StageHandle()
    if not logger.enabled:
        yield handle
        return
    started_event = f"{trace_event_base}.started"
    completed_event = f"{trace_event_base}.completed"
    failed_event = f"{trace_event_base}.failed"

    logger.emit(
        trace_event=started_event,
        stage=stage,
        status="started",
        context=context,
    )
    started_clock = time.monotonic()
    try:
        yield handle
    except BaseException as exc:
        duration_ms = _elapsed_ms(started_clock)
        slow = duration_ms >= logger.slow_stage_ms
        meta = _merge_metadata(metadata_builder, handle._metadata)
        logger.emit(
            trace_event=failed_event,
            stage=stage,
            status="failed",
            context=context,
            duration_ms=duration_ms,
            slow=slow,
            error_type=type(exc).__name__,
            error_message=_str_or_none(exc),
            metadata=meta,
        )
        if slow:
            logger.emit_slow_warning(
                trace_event=failed_event,
                stage=stage,
                duration_ms=duration_ms,
                context=context,
            )
        raise
    else:
        duration_ms = _elapsed_ms(started_clock)
        slow = duration_ms >= logger.slow_stage_ms
        meta = _merge_metadata(metadata_builder, handle._metadata)
        logger.emit(
            trace_event=completed_event,
            stage=stage,
            status="completed",
            context=context,
            duration_ms=duration_ms,
            slow=slow,
            metadata=meta,
        )
        if slow:
            logger.emit_slow_warning(
                trace_event=completed_event,
                stage=stage,
                duration_ms=duration_ms,
                context=context,
            )


@dataclass
class StageHandle:
    """Lets a stage attach late-discovered metadata to the eventual
    ``completed`` / ``failed`` event without rebuilding the whole
    payload at exit time.
    """

    _metadata: dict[str, Any] = field(default_factory=dict)

    def set_metadata(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if value is None:
                continue
            self._metadata[key] = value


# ---- Helpers ----------------------------------------------------------


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _elapsed_ms(started_clock: float) -> int:
    return int((time.monotonic() - started_clock) * 1000)


def _str_or_none(exc: BaseException) -> str | None:
    try:
        msg = str(exc)
    except Exception:  # noqa: BLE001 — defensive; some exceptions stringify poorly
        msg = type(exc).__name__
    if not msg:
        return None
    return msg


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _sanitize_metadata(
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Strip unsafe keys and truncate long string values.

    Top-level dict-of-dict is supported; nested dicts also pass through
    the same key blacklist + value truncation. Lists are passed
    through unchanged when they contain only primitives; lists of
    dicts go through the same recursion. Anything else is coerced to
    its truncated string representation.
    """
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in _UNSAFE_METADATA_KEYS:
            continue
        out[key] = _sanitize_value(value)
    return out


def _sanitize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value, _METADATA_VALUE_TRUNCATE_CHARS)
    if isinstance(value, Mapping):
        return _sanitize_metadata(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    # Anything else (bytes, custom objects) is coerced + truncated so
    # large structures never reach the JSONL file untruncated.
    return _truncate(repr(value), _METADATA_VALUE_TRUNCATE_CHARS)


def _resolve_metadata(
    metadata: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    if metadata is None:
        return None
    if callable(metadata):
        try:
            built = metadata()
        except Exception:  # noqa: BLE001 — never let metadata build fail the trace
            return None
        return built
    return metadata


def _merge_metadata(
    builder: Callable[[], Mapping[str, Any]] | None,
    handle_meta: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Merge a late-discovered ``StageHandle`` payload with the
    builder's lazily-built map. Handle values take precedence."""
    built: Mapping[str, Any] | None = None
    if builder is not None:
        try:
            built = builder()
        except Exception:  # noqa: BLE001 — defensive
            built = None
    if not built and not handle_meta:
        return None
    merged: dict[str, Any] = {}
    if built:
        merged.update(built)
    if handle_meta:
        merged.update(handle_meta)
    return merged


# ---- Env parsing (private; kept local to avoid a settings dependency) -


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
