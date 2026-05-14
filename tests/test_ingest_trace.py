"""Tests for the dedicated ingestion performance trace.

Covers brief §9: disabled by default, enabled emits events, timed
stages carry duration, slow stages flag + emit a warning, failed
stages capture safe error info, unsafe metadata keys are stripped,
correlation fields propagate, and an end-to-end smoke test via
:class:`DiagnosticRecorder` exercises the started/completed pair.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from j1.observability.ingest_trace import (
    ENV_INGEST_TRACE_ENABLED,
    ENV_INGEST_TRACE_OUTPUT,
    ENV_INGEST_TRACE_SLOW_STAGE_MS,
    IngestTraceLogger,
    IngestTraceSettings,
    TraceContext,
    current_ingest_trace_logger,
    load_ingest_trace_settings,
    reset_ingest_trace_logger,
    trace_event,
    trace_stage,
)


@pytest.fixture
def trace_path(tmp_path: Path) -> Path:
    return tmp_path / "ingest_trace.jsonl"


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines() if line.strip()
    ]


def _install_logger(path: Path, *, enabled: bool, slow_ms: int = 10_000):
    settings = IngestTraceSettings(
        enabled=enabled,
        output_path=str(path),
        slow_stage_ms=slow_ms,
    )
    logger = IngestTraceLogger(settings)
    reset_ingest_trace_logger(logger)
    return logger


@pytest.fixture(autouse=True)
def _clean_singleton():
    """Each test starts with the singleton dropped so an enabling test
    can't bleed state into a disabling test (or vice versa)."""
    reset_ingest_trace_logger(None)
    yield
    reset_ingest_trace_logger(None)


# ---- 1. Disabled by default ----------------------------------------


def test_disabled_by_default_emits_no_file(trace_path: Path, monkeypatch):
    """With ``J1_INGEST_TRACE_ENABLED`` unset, the loader resolves
    ``enabled=False`` and the module-level helpers must not create
    the JSONL file even when called repeatedly."""
    monkeypatch.delenv(ENV_INGEST_TRACE_ENABLED, raising=False)
    monkeypatch.setenv(ENV_INGEST_TRACE_OUTPUT, str(trace_path))

    settings = load_ingest_trace_settings()
    assert settings.enabled is False

    # Even with the singleton initialised from env, an event call
    # should be a no-op when disabled.
    logger = current_ingest_trace_logger()
    assert logger.enabled is False
    trace_event(
        trace_event="ingest.test.fired",
        stage="test",
        status="completed",
        context=TraceContext(run_id="r-1"),
    )
    with trace_stage(
        trace_event_base="ingest.test", stage="test",
        context=TraceContext(run_id="r-1"),
    ):
        pass
    assert not trace_path.exists()


# ---- 2. Enabled trace emits events ---------------------------------


def test_enabled_emits_started_and_completed(trace_path: Path):
    _install_logger(trace_path, enabled=True)
    with trace_stage(
        trace_event_base="ingest.compile",
        stage="compile",
        context=TraceContext(run_id="r-1", document_id="d-1"),
    ):
        pass
    lines = _read_lines(trace_path)
    assert [ln["trace_event"] for ln in lines] == [
        "ingest.compile.started", "ingest.compile.completed",
    ]
    assert all(ln["stage"] == "compile" for ln in lines)
    assert lines[0]["status"] == "started"
    assert lines[1]["status"] == "completed"


# ---- 3. Timed stage includes duration ------------------------------


def test_completed_event_includes_duration_and_slow_flag(
    trace_path: Path,
):
    _install_logger(trace_path, enabled=True, slow_ms=10_000)
    with trace_stage(
        trace_event_base="ingest.compile",
        stage="compile",
        context=TraceContext(run_id="r-1"),
    ):
        pass
    [_, completed] = _read_lines(trace_path)
    assert "duration_ms" in completed
    assert isinstance(completed["duration_ms"], int)
    assert completed["duration_ms"] >= 0
    assert completed["slow"] is False


# ---- 4. Slow stage detection --------------------------------------


def test_slow_stage_sets_slow_true_and_warns(
    trace_path: Path, caplog,
):
    _install_logger(trace_path, enabled=True, slow_ms=1)
    with caplog.at_level(
        logging.WARNING, logger="j1.ingest_trace.slow_stage",
    ):
        with trace_stage(
            trace_event_base="ingest.compile",
            stage="compile",
            context=TraceContext(run_id="r-1", document_id="d-1"),
        ):
            time.sleep(0.02)
    [_, completed] = _read_lines(trace_path)
    assert completed["slow"] is True
    assert completed["duration_ms"] >= 1
    # One warning on the normal logger so operators can grep it.
    warn_messages = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warn_messages) == 1
    rec = warn_messages[0]
    assert rec.getMessage() == "ingest.trace.slow_stage"
    assert getattr(rec, "stage", None) == "compile"
    assert getattr(rec, "run_id", None) == "r-1"
    assert getattr(rec, "threshold_ms", None) == 1


# ---- 5. Failed stage logs safe error ------------------------------


def test_failed_stage_emits_failed_event_with_short_error(
    trace_path: Path,
):
    _install_logger(trace_path, enabled=True)
    long_message = "boom! " * 200
    with pytest.raises(RuntimeError):
        with trace_stage(
            trace_event_base="ingest.compile",
            stage="compile",
            context=TraceContext(run_id="r-1"),
        ):
            raise RuntimeError(long_message)
    [_, failed] = _read_lines(trace_path)
    assert failed["trace_event"] == "ingest.compile.failed"
    assert failed["status"] == "failed"
    assert failed["error_type"] == "RuntimeError"
    # Error message is included and truncated (it's well over the
    # 300-char cap so the helper should append the ellipsis sentinel).
    assert "error_message" in failed
    assert len(failed["error_message"]) <= 301
    assert "duration_ms" in failed


# ---- 6. No unsafe payload leakage ---------------------------------


def test_unsafe_metadata_keys_are_stripped(trace_path: Path):
    _install_logger(trace_path, enabled=True)
    trace_event(
        trace_event="ingest.test.fired",
        stage="test",
        status="completed",
        context=TraceContext(run_id="r-1"),
        metadata={
            "text": "FULL DOCUMENT BODY",
            "content": "more body",
            "chunks": [{"text": "x"}],
            "embedding": [0.1, 0.2],
            "embeddings": [[0.1]],
            "prompt": "system: ...",
            "prompts": ["sys"],
            "response": "model output",
            "responses": ["out"],
            "ocr_output": "...",
            "image_bytes": b"\x89PNG",
            "raw_bytes": b"...",
            # Allowed summaries:
            "artifacts_count": 12,
            "parser": "mineru",
        },
    )
    [event] = _read_lines(trace_path)
    meta = event.get("metadata") or {}
    for unsafe in (
        "text", "content", "chunks", "embedding", "embeddings",
        "prompt", "prompts", "response", "responses", "ocr_output",
        "image_bytes", "raw_bytes",
    ):
        assert unsafe not in meta, f"unsafe key {unsafe!r} leaked"
    assert meta["artifacts_count"] == 12
    assert meta["parser"] == "mineru"


def test_long_string_metadata_values_are_truncated(trace_path: Path):
    """Even a non-blacklisted key has a hard 240-char cap so a stray
    long string can't bloat the trace file."""
    _install_logger(trace_path, enabled=True)
    long_value = "x" * 500
    trace_event(
        trace_event="ingest.test.fired",
        stage="test",
        status="completed",
        context=TraceContext(run_id="r-1"),
        metadata={"reason": long_value},
    )
    [event] = _read_lines(trace_path)
    assert len(event["metadata"]["reason"]) <= 241  # 240 + ellipsis


# ---- 7. Correlation fields ----------------------------------------


def test_correlation_fields_propagate(trace_path: Path):
    _install_logger(trace_path, enabled=True)
    trace_event(
        trace_event="ingest.test.fired",
        stage="test",
        status="completed",
        context=TraceContext(
            tenant_id="t", project_id="p", document_id="d-1",
            run_id="r-1", target_snapshot_id="snap-1",
            workflow_id="wf-1", activity="compile_document", attempt=2,
        ),
    )
    [event] = _read_lines(trace_path)
    assert event["tenant_id"] == "t"
    assert event["project_id"] == "p"
    assert event["document_id"] == "d-1"
    assert event["run_id"] == "r-1"
    assert event["target_snapshot_id"] == "snap-1"
    assert event["workflow_id"] == "wf-1"
    assert event["activity"] == "compile_document"
    assert event["attempt"] == 2


# ---- 8. Ingestion path smoke test ---------------------------------


def test_diagnostic_recorder_stage_emits_paired_events(
    trace_path: Path,
):
    """End-to-end via ``DiagnosticRecorder.stage()`` — the canonical
    ingest timing surface. Confirms the helper is wired (the
    integration in :mod:`j1.processing.diagnostics` calls
    ``trace_event`` on stage entry + exit)."""
    _install_logger(trace_path, enabled=True)

    from j1.processing.diagnostics import DiagnosticRecorder
    from j1.projects.context import ProjectContext

    recorder = DiagnosticRecorder()  # no audit / artifacts wired
    ctx = ProjectContext(tenant_id="t", project_id="p")
    with recorder.stage(
        ctx=ctx, run_id="r-smoke", stage_name="compile",
        document_id="d-smoke",
    ) as handle:
        handle.update(chunk_count=3)

    events = _read_lines(trace_path)
    names = [e["trace_event"] for e in events]
    assert names == [
        "ingest.compile.started", "ingest.compile.completed",
    ]
    completed = events[-1]
    assert completed["stage"] == "compile"
    assert completed["run_id"] == "r-smoke"
    assert completed["document_id"] == "d-smoke"
    assert completed["project_id"] == "p"
    assert completed["tenant_id"] == "t"
    assert "duration_ms" in completed


# ---- Bonus: lazy metadata is not built when disabled --------------


def test_disabled_does_not_call_metadata_builder(trace_path: Path):
    """The expensive ``metadata_builder=lambda: ...`` form must NOT be
    invoked when trace is disabled — otherwise the disabled path
    pays for things like artifact counters every time."""
    _install_logger(trace_path, enabled=False)

    calls = {"count": 0}

    def builder():
        calls["count"] += 1
        return {"artifact_count": 0}

    with trace_stage(
        trace_event_base="ingest.compile",
        stage="compile",
        context=TraceContext(run_id="r-1"),
        metadata_builder=builder,
    ):
        pass

    assert calls["count"] == 0
