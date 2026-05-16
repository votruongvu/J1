"""Per-compile ingest performance trace.

A small accumulator that lives alongside the LLM-call audit
([`_llm_audit.py`](./_llm_audit.py)). The audit module emits
*per-call* start/complete events; this module rolls those calls
(plus parse / embedding wall-clock samples) into a *per-compile*
summary so an operator can answer:

   "how long did indexing this document take, and where did the
    time go — MinerU parse, graph extraction LLM calls, embedding,
    or LightRAG's insert bookkeeping?"

The trace is a plain dataclass passed by reference through the
RAGAnything compile pipeline. Call sites stamp samples onto it:

   trace.record_stage("parse", elapsed_ms=12345)
   trace.record_llm_call(stage="graph_extraction", model="…", usage=...)
   trace.record_embedding(elapsed_ms=42, model="…", tokens=128)

At the end of compile the bridge calls `trace.emit_summary()` which
logs a single ``ingest.perf.summary`` event AND returns a dict to
fold into ``compile_metadata["perf"]``. Downstream consumers
(activity-layer report, run-detail panel) read it from there.

Why not OpenTelemetry: the rest of the framework emits structured
log events for stage transitions; perf data follows the same
convention so dashboards built on the existing log aggregator
keep working without an APM rollout.

Hygiene: nothing in this module touches prompts, system prompts,
or response bodies. The summary surface is exactly: stage
wall-clock ms, LLM-call counts + token totals, embedding-call
counts + token totals, and the set of model names that fired per
stage. Operator-readable and aggregate-only.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from j1.llm.clients import LLMUsage


_log = logging.getLogger(__name__)


# Stable stage vocabulary. Dashboards filter on these — add new
# values, don't rename. Mirrors the purpose vocabulary in
# `_llm_audit.py` at a coarser granularity.
STAGE_PARSE = "parse"
STAGE_GRAPH_EXTRACTION = "graph_extraction"
STAGE_VISION_ANALYSIS = "vision_analysis"
STAGE_EMBEDDING = "embedding"
STAGE_INSERT = "insert"


@dataclass
class _LLMCallTally:
    """Rolling LLM-call totals scoped to a single stage."""

    stage: str
    call_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    # Models that fired in this stage. A set so re-fires of the
    # same model don't inflate the summary; the surface is "which
    # models did work here" not "how often".
    models: set[str] = field(default_factory=set)

    def to_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "call_count": self.call_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "total_duration_ms": self.total_duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "models": sorted(self.models),
        }


@dataclass
class _StageWallClock:
    """Wall-clock ms tallied per stage outside the LLM-call hot path
    (parse + insert + embedding stretches the audit wrapper can't
    see)."""

    stage: str
    elapsed_ms: int = 0
    sample_count: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "elapsed_ms": self.elapsed_ms,
            "sample_count": self.sample_count,
        }


class IngestPerfTrace:
    """Per-compile performance trace.

    Thread-safe via a single internal lock — RAGAnything fires LLM
    callables from worker threads (`asyncio.to_thread`), so token
    + duration samples land from multiple threads. The lock scope
    is per-sample (constant work) — never holds across the actual
    LLM call.
    """

    def __init__(
        self,
        *,
        document_id: str,
        run_id: str | None = None,
        selected_profile: str | None = None,
    ) -> None:
        self._document_id = document_id
        self._run_id = run_id
        self._selected_profile = selected_profile
        self._lock = threading.Lock()
        self._llm_tallies: dict[str, _LLMCallTally] = {}
        self._stage_clocks: dict[str, _StageWallClock] = {}
        self._compile_start_ns = time.monotonic_ns()
        self._emitted = False

    # ---- Sample APIs --------------------------------------------------

    def record_stage(self, stage: str, *, elapsed_ms: int) -> None:
        """Add a wall-clock sample for ``stage``. Mostly used for
        parse + insert + embedding stretches that aren't a single
        LLM call. Multiple samples accumulate."""
        with self._lock:
            clock = self._stage_clocks.setdefault(
                stage, _StageWallClock(stage=stage),
            )
            clock.elapsed_ms += int(elapsed_ms)
            clock.sample_count += 1

    def record_llm_call(
        self,
        *,
        stage: str,
        model: str | None,
        duration_ms: int,
        success: bool,
    ) -> None:
        """Record one LLM call's *outcome* — duration + success
        bucket + the model that fired. Token usage goes through
        ``record_llm_usage`` separately so the audit wrapper (which
        sees duration but not token counts) and the underlying
        callable (which sees tokens via the client's ``LLMUsage``
        but not the wrap-side duration) can each contribute their
        side without double-counting the call_count column.

        Call this exactly ONCE per LLM call — from the audit
        wrapper. ``record_llm_usage`` is what the callable layer
        uses to fold in the token side."""
        with self._lock:
            tally = self._llm_tallies.setdefault(
                stage, _LLMCallTally(stage=stage),
            )
            tally.call_count += 1
            if success:
                tally.success_count += 1
            else:
                tally.failure_count += 1
            tally.total_duration_ms += int(duration_ms)
            if model:
                tally.models.add(model)

    def record_llm_usage(
        self,
        *,
        stage: str,
        model: str | None,
        usage: LLMUsage,
    ) -> None:
        """Fold a ``LLMUsage`` record's token counts into ``stage``.
        Does NOT bump ``call_count`` (the audit wrapper owns that
        column via ``record_llm_call``). Called from the J1-side
        text / vision callable, which is the only layer that sees
        token counts — RAGAnything drops them at the vendor
        boundary."""
        with self._lock:
            tally = self._llm_tallies.setdefault(
                stage, _LLMCallTally(stage=stage),
            )
            if model:
                tally.models.add(model)
            tally.input_tokens += int(usage.input_tokens)
            tally.output_tokens += int(usage.output_tokens)
            tally.total_tokens += int(usage.total_tokens)

    @contextmanager
    def stage_timer(self, stage: str) -> Iterator[None]:
        """Context manager that records elapsed ms onto ``stage``
        when the block exits — success or exception. Useful for
        parse / insert / embedding stretches.

        Multiple opened timers on the same stage accumulate
        independently (no double-counting); the outermost timer
        sees the whole window, the inner ones see their nested
        portion, and each samples once into its stage's clock."""
        start = time.monotonic_ns()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic_ns() - start) // 1_000_000
            self.record_stage(stage, elapsed_ms=int(elapsed_ms))

    # ---- Summary surface ---------------------------------------------

    def total_compile_elapsed_ms(self) -> int:
        return (time.monotonic_ns() - self._compile_start_ns) // 1_000_000

    def to_summary(self) -> dict[str, object]:
        """Return the per-compile perf summary as a plain dict.
        Safe to call multiple times; doesn't emit the log line."""
        with self._lock:
            stages = sorted(self._stage_clocks)
            llm_stages = sorted(self._llm_tallies)
            stage_clocks_payload = [
                self._stage_clocks[name].to_payload() for name in stages
            ]
            llm_tallies_payload = [
                self._llm_tallies[name].to_payload() for name in llm_stages
            ]
            all_models: set[str] = set()
            total_input = 0
            total_output = 0
            total_calls = 0
            total_llm_duration_ms = 0
            for tally in self._llm_tallies.values():
                all_models.update(tally.models)
                total_input += tally.input_tokens
                total_output += tally.output_tokens
                total_calls += tally.call_count
                total_llm_duration_ms += tally.total_duration_ms
        return {
            "document_id": self._document_id,
            "run_id": self._run_id,
            "selected_profile": self._selected_profile,
            "compile_elapsed_ms": self.total_compile_elapsed_ms(),
            "stage_clocks": stage_clocks_payload,
            "llm_calls": llm_tallies_payload,
            "totals": {
                "llm_call_count": total_calls,
                "llm_total_duration_ms": total_llm_duration_ms,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "models_used": sorted(all_models),
            },
        }

    def emit_summary(self) -> dict[str, object]:
        """Log a single ``ingest.perf.summary`` event and return
        the payload. Idempotent — only the first call emits, so
        the bridge can fire it in a ``finally`` block without
        double-counting if a caller invokes it earlier."""
        payload = self.to_summary()
        if self._emitted:
            return payload
        self._emitted = True
        _log.info(
            "ingest.perf.summary",
            extra={"event": "ingest.perf.summary", **payload},
        )
        return payload
