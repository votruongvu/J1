"""Contract — per-compile ingest performance trace.

Pins the surface of `IngestPerfTrace` and its interaction with
`wrap_audited_async` so the per-compile perf-summary payload
remains a stable shape for dashboards.

The trace splits its accounting between two APIs:

  * `record_llm_call(stage, model, duration_ms, success)` — bumps
    call_count + duration + model set. Owned by the audit wrapper
    (which sees per-call duration but not token counts).
  * `record_llm_usage(stage, model, usage)` — folds token counts
    in. Owned by the J1-side callable (which sees `LLMUsage`
    via the underlying client; RAGAnything drops it).

The split exists to avoid double-counting `call_count` when both
the audit wrapper and the callable contribute samples for the
same LLM call.
"""

from __future__ import annotations

import asyncio
import logging

from j1.llm.clients import LLMUsage
from j1.providers.raganything._llm_audit import (
    LLMAuditConfig,
    PURPOSE_ENTITY_EXTRACTION,
    wrap_audited_async,
)
from j1.providers.raganything._perf_trace import (
    STAGE_EMBEDDING,
    STAGE_GRAPH_EXTRACTION,
    STAGE_INSERT,
    STAGE_PARSE,
    STAGE_VISION_ANALYSIS,
    IngestPerfTrace,
)


def _trace(**kw) -> IngestPerfTrace:
    return IngestPerfTrace(
        document_id=kw.get("document_id", "doc-1"),
        run_id=kw.get("run_id", "run-1"),
        selected_profile=kw.get("selected_profile", "knowledge_index"),
    )


def _usage(input_tokens=10, output_tokens=20, model="m") -> LLMUsage:
    return LLMUsage(
        provider="stub",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


# ---- Stage clocks ------------------------------------------------


def test_record_stage_accumulates_elapsed_ms():
    trace = _trace()
    trace.record_stage(STAGE_PARSE, elapsed_ms=120)
    trace.record_stage(STAGE_PARSE, elapsed_ms=80)
    summary = trace.to_summary()
    clocks = {c["stage"]: c for c in summary["stage_clocks"]}
    assert clocks[STAGE_PARSE]["elapsed_ms"] == 200
    assert clocks[STAGE_PARSE]["sample_count"] == 2


def test_stage_timer_context_records_elapsed():
    trace = _trace()
    import time
    with trace.stage_timer(STAGE_INSERT):
        time.sleep(0.01)
    summary = trace.to_summary()
    clocks = {c["stage"]: c for c in summary["stage_clocks"]}
    assert STAGE_INSERT in clocks
    assert clocks[STAGE_INSERT]["sample_count"] == 1
    assert clocks[STAGE_INSERT]["elapsed_ms"] >= 5


def test_stage_timer_records_on_exception():
    trace = _trace()
    try:
        with trace.stage_timer(STAGE_PARSE):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    summary = trace.to_summary()
    clocks = {c["stage"]: c for c in summary["stage_clocks"]}
    assert STAGE_PARSE in clocks
    assert clocks[STAGE_PARSE]["sample_count"] == 1


# ---- LLM-call accounting -----------------------------------------


def test_record_llm_call_bumps_count_and_duration_and_models():
    trace = _trace()
    trace.record_llm_call(
        stage=STAGE_GRAPH_EXTRACTION,
        model="m1",
        duration_ms=42,
        success=True,
    )
    trace.record_llm_call(
        stage=STAGE_GRAPH_EXTRACTION,
        model="m1",
        duration_ms=58,
        success=False,
    )
    summary = trace.to_summary()
    calls = {c["stage"]: c for c in summary["llm_calls"]}
    g = calls[STAGE_GRAPH_EXTRACTION]
    assert g["call_count"] == 2
    assert g["success_count"] == 1
    assert g["failure_count"] == 1
    assert g["total_duration_ms"] == 100
    assert g["models"] == ["m1"]


def test_record_llm_usage_does_not_bump_call_count():
    """Token-side recording must NOT bump call_count — the audit
    wrapper owns that column. Double-counting would inflate the
    dashboard's per-document call totals."""
    trace = _trace()
    trace.record_llm_call(
        stage=STAGE_GRAPH_EXTRACTION,
        model="m1",
        duration_ms=42,
        success=True,
    )
    trace.record_llm_usage(
        stage=STAGE_GRAPH_EXTRACTION,
        model="m1",
        usage=_usage(input_tokens=100, output_tokens=200),
    )
    summary = trace.to_summary()
    calls = {c["stage"]: c for c in summary["llm_calls"]}
    g = calls[STAGE_GRAPH_EXTRACTION]
    assert g["call_count"] == 1
    assert g["input_tokens"] == 100
    assert g["output_tokens"] == 200
    assert g["total_tokens"] == 300


def test_record_llm_usage_collects_distinct_models():
    """When two different models fire in the same stage (e.g.
    graph extraction routed through different model versions during
    a compile), the model set should include both."""
    trace = _trace()
    trace.record_llm_usage(
        stage=STAGE_GRAPH_EXTRACTION, model="m1",
        usage=_usage(),
    )
    trace.record_llm_usage(
        stage=STAGE_GRAPH_EXTRACTION, model="m2",
        usage=_usage(),
    )
    summary = trace.to_summary()
    calls = {c["stage"]: c for c in summary["llm_calls"]}
    assert calls[STAGE_GRAPH_EXTRACTION]["models"] == ["m1", "m2"]


# ---- Audit-wrapper integration -----------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_audit_wrapper_records_call_when_perf_trace_present_audit_on():
    """With auditing enabled AND perf_trace present, the wrapper
    must record one call onto the trace per invocation, with the
    actual duration."""
    trace = _trace()

    async def inner():
        return "ok"

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION,
        stage="compile",
        provider="stub",
        model="m1",
        selected_profile="knowledge_index",
        config=LLMAuditConfig(enabled=True),
        perf_trace=trace,
        perf_stage=STAGE_GRAPH_EXTRACTION,
    )
    _run(wrapped())
    _run(wrapped())
    summary = trace.to_summary()
    calls = {c["stage"]: c for c in summary["llm_calls"]}
    assert calls[STAGE_GRAPH_EXTRACTION]["call_count"] == 2
    assert calls[STAGE_GRAPH_EXTRACTION]["success_count"] == 2
    assert calls[STAGE_GRAPH_EXTRACTION]["models"] == ["m1"]


def test_audit_wrapper_records_call_when_perf_trace_present_audit_off():
    """Audit OFF (env flag unset) should STILL feed the perf trace
    when wired — the operator may want perf tracing without log-spam.
    The trace doesn't generate per-call log lines; it's a different
    dial than `J1_LLM_CALL_AUDIT_ENABLED`."""
    trace = _trace()

    async def inner():
        return "ok"

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION,
        stage="compile",
        provider="stub",
        model="m1",
        selected_profile="knowledge_index",
        config=LLMAuditConfig(enabled=False),
        perf_trace=trace,
        perf_stage=STAGE_GRAPH_EXTRACTION,
    )
    _run(wrapped())
    summary = trace.to_summary()
    calls = {c["stage"]: c for c in summary["llm_calls"]}
    assert calls[STAGE_GRAPH_EXTRACTION]["call_count"] == 1


def test_audit_wrapper_marks_failure_on_exception():
    trace = _trace()

    async def inner():
        raise RuntimeError("upstream 500")

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION,
        stage="compile",
        provider="stub",
        model="m1",
        selected_profile="knowledge_index",
        config=LLMAuditConfig(enabled=True),
        perf_trace=trace,
        perf_stage=STAGE_GRAPH_EXTRACTION,
    )
    import pytest

    with pytest.raises(RuntimeError):
        _run(wrapped())
    summary = trace.to_summary()
    calls = {c["stage"]: c for c in summary["llm_calls"]}
    g = calls[STAGE_GRAPH_EXTRACTION]
    assert g["call_count"] == 1
    assert g["failure_count"] == 1
    assert g["success_count"] == 0


def test_audit_wrapper_no_perf_trace_does_not_break():
    """Existing call sites that don't supply a perf trace must
    still work — the parameter is optional."""
    async def inner():
        return "ok"

    wrapped = wrap_audited_async(
        inner,
        purpose=PURPOSE_ENTITY_EXTRACTION,
        stage="compile",
        provider="stub",
        model="m1",
        selected_profile="knowledge_index",
        config=LLMAuditConfig(enabled=True),
    )
    assert _run(wrapped()) == "ok"


# ---- Summary payload shape ---------------------------------------


def test_summary_carries_document_run_profile_identifiers():
    trace = _trace(
        document_id="doc-xyz",
        run_id="run-42",
        selected_profile="knowledge_index",
    )
    summary = trace.to_summary()
    assert summary["document_id"] == "doc-xyz"
    assert summary["run_id"] == "run-42"
    assert summary["selected_profile"] == "knowledge_index"


def test_summary_totals_aggregate_across_stages():
    """The `totals` block sums call_count + tokens + duration across
    every stage so the operator sees one number per metric without
    summing the per-stage rows themselves."""
    trace = _trace()
    trace.record_llm_call(
        stage=STAGE_GRAPH_EXTRACTION, model="m1",
        duration_ms=10, success=True,
    )
    trace.record_llm_usage(
        stage=STAGE_GRAPH_EXTRACTION, model="m1",
        usage=_usage(input_tokens=100, output_tokens=200),
    )
    trace.record_llm_call(
        stage=STAGE_VISION_ANALYSIS, model="vl1",
        duration_ms=30, success=True,
    )
    trace.record_llm_usage(
        stage=STAGE_VISION_ANALYSIS, model="vl1",
        usage=_usage(input_tokens=50, output_tokens=10),
    )
    summary = trace.to_summary()
    totals = summary["totals"]
    assert totals["llm_call_count"] == 2
    assert totals["llm_total_duration_ms"] == 40
    assert totals["input_tokens"] == 150
    assert totals["output_tokens"] == 210
    assert totals["total_tokens"] == 360
    # Models from both stages folded together.
    assert totals["models_used"] == ["m1", "vl1"]


def test_emit_summary_logs_perf_event_once(caplog):
    trace = _trace()
    trace.record_stage(STAGE_PARSE, elapsed_ms=100)
    trace.record_llm_call(
        stage=STAGE_GRAPH_EXTRACTION, model="m1",
        duration_ms=10, success=True,
    )
    from j1.providers.raganything import _perf_trace as pt
    with caplog.at_level(logging.INFO, logger=pt._log.name):
        payload1 = trace.emit_summary()
        payload2 = trace.emit_summary()  # idempotent
    events = [r.__dict__.get("event") for r in caplog.records]
    assert events.count("ingest.perf.summary") == 1
    assert payload1["totals"]["llm_call_count"] == 1
    # Second call returns the same payload but doesn't re-emit.
    assert payload2["totals"]["llm_call_count"] == 1


def test_summary_shape_for_dashboard_consumers():
    """Pins the top-level key set so dashboards / FE consumers can
    rely on the surface. Add new keys, don't rename existing ones."""
    trace = _trace()
    summary = trace.to_summary()
    expected = {
        "document_id", "run_id", "selected_profile",
        "compile_elapsed_ms", "stage_clocks", "llm_calls", "totals",
    }
    assert expected.issubset(summary.keys())
    totals_keys = {
        "llm_call_count", "llm_total_duration_ms",
        "input_tokens", "output_tokens", "total_tokens", "models_used",
    }
    assert totals_keys.issubset(summary["totals"].keys())


def test_empty_trace_summary_is_well_formed():
    """A compile that never fires an LLM (e.g. minimum_queryable with
    the no-op callable that doesn't actually invoke an LLM, OR a
    failed compile that bombs before parse) still produces a
    consistent summary — every key present, sums all zero."""
    trace = _trace()
    summary = trace.to_summary()
    assert summary["stage_clocks"] == []
    assert summary["llm_calls"] == []
    assert summary["totals"]["llm_call_count"] == 0
    assert summary["totals"]["total_tokens"] == 0
    assert summary["totals"]["models_used"] == []
