"""Tests for the Phase-1 ingestion diagnostic recorder.

Pins the contract every downstream consumer relies on:

  * ``stage`` context manager records started/completed/duration
    and emits two structured audit events with the stable
    ``j1.ingestion.stage.*`` action names.
  * ``record_llm_call`` aggregates calls into the per-run report
    and emits ``j1.ingestion.llm_call.completed``.
  * ``record_enrichment_progress`` updates the running summary
    (planned / completed / skipped / failed / status).
  * ``write_report`` builds the artifact + clears in-memory state
    so a second write for the same run is a no-op.
  * Failures inside the recorder never propagate to callers — the
    stage wrap returns / re-raises the underlying exception
    cleanly even when the audit emit explodes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.processing.diagnostics import (
    ARTIFACT_KIND_DIAGNOSTIC_REPORT,
    EVENT_LLM_CALL_COMPLETED,
    EVENT_REPORT_WRITTEN,
    EVENT_STAGE_COMPLETED,
    EVENT_STAGE_STARTED,
    DiagnosticRecorder,
)


# ---- Helpers -----------------------------------------------------


class _SpyAudit:
    """Captures every ``record(...)`` call so tests can assert on
    the emitted action names + payloads."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, ctx, *, actor, action, target_kind, target_id, payload):
        self.events.append({
            "actor": actor,
            "action": action,
            "target_kind": target_kind,
            "target_id": target_id,
            "payload": dict(payload),
        })


def _make_recorder(workspace, artifact_registry, audit=None):
    return DiagnosticRecorder(
        audit=audit,
        artifact_registry=artifact_registry,
        workspace=workspace,
        clock=lambda: datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---- Stage timing ------------------------------------------------


def test_stage_context_manager_records_duration_and_events(
    ctx, workspace, artifact_registry,
):
    audit = _SpyAudit()
    rec = _make_recorder(workspace, artifact_registry, audit=audit)

    with rec.stage(
        ctx=ctx, run_id="r-1", stage_name="compile",
        counters={"page_count": 12},
    ) as stage:
        stage.update(chunk_count=120)

    actions = [e["action"] for e in audit.events]
    assert EVENT_STAGE_STARTED in actions
    assert EVENT_STAGE_COMPLETED in actions
    completed = next(e for e in audit.events if e["action"] == EVENT_STAGE_COMPLETED)
    assert completed["target_id"] == "r-1"
    assert completed["payload"]["stage"] == "compile"
    assert completed["payload"]["success"] is True
    # ``duration_ms`` is a real wall-clock measurement — assert
    # it's present + nonneg, not an exact value.
    assert completed["payload"]["duration_ms"] is not None
    assert completed["payload"]["duration_ms"] >= 0
    assert completed["payload"]["counters"]["chunk_count"] == 120
    assert completed["payload"]["counters"]["page_count"] == 12


def test_stage_records_failure_with_exception_type_only(
    ctx, workspace, artifact_registry,
):
    """Stage on the failure branch records ``success=False`` and
    the EXCEPTION CLASS NAME — never the message body, which could
    contain a snippet of document content."""
    audit = _SpyAudit()
    rec = _make_recorder(workspace, artifact_registry, audit=audit)

    with pytest.raises(ValueError):
        with rec.stage(ctx=ctx, run_id="r-1", stage_name="parse"):
            raise ValueError("a leaked chunk of the document body...")

    completed = next(
        e for e in audit.events if e["action"] == EVENT_STAGE_COMPLETED
    )
    assert completed["payload"]["success"] is False
    # The error field carries the exception class name only.
    assert completed["payload"]["error"] == "ValueError"


def test_stage_with_no_run_id_is_noop(ctx, workspace, artifact_registry):
    """``run_id=None`` is the no-recorder fallback for unwired
    paths — the with-block runs but nothing is captured."""
    audit = _SpyAudit()
    rec = _make_recorder(workspace, artifact_registry, audit=audit)

    with rec.stage(ctx=ctx, run_id=None, stage_name="compile"):
        pass
    assert audit.events == []
    assert rec.build_report("any-id") is None


def test_record_stage_event_for_pre_measured_duration(
    ctx, workspace, artifact_registry,
):
    """Code paths that measured their own timing (e.g. the MinerU
    bridge tracks parse_elapsed_ms) feed the recorder via
    ``record_stage_event`` rather than the context manager."""
    audit = _SpyAudit()
    rec = _make_recorder(workspace, artifact_registry, audit=audit)

    rec.record_stage_event(
        ctx=ctx, run_id="r-1", stage_name="parse",
        duration_ms=1480000, counters={"page_count": 89},
    )
    actions = [e["action"] for e in audit.events]
    assert actions == [EVENT_STAGE_STARTED, EVENT_STAGE_COMPLETED]
    payload = audit.events[1]["payload"]
    assert payload["duration_ms"] == 1480000
    assert payload["counters"]["page_count"] == 89


# ---- LLM call accumulation ---------------------------------------


def test_record_llm_call_aggregates_into_report(
    ctx, workspace, artifact_registry,
):
    audit = _SpyAudit()
    rec = _make_recorder(workspace, artifact_registry, audit=audit)

    rec.record_llm_call(
        ctx=ctx, run_id="r-1", stage="compile", purpose="chunk_metadata",
        provider="openai_compat", model="gemini-2.5-pro",
        duration_ms=1234, input_tokens=420, output_tokens=80,
        attempts=1, retried=False,
    )
    rec.record_llm_call(
        ctx=ctx, run_id="r-1", stage="enrich", purpose="enrichment.images",
        provider="openai_compat", model="gemini-2.5-pro",
        duration_ms=856, input_tokens=120, output_tokens=40,
        attempts=2, retried=True, error="TimeoutError",
    )

    actions = [e["action"] for e in audit.events]
    assert actions.count(EVENT_LLM_CALL_COMPLETED) == 2

    report = rec.build_report("r-1")
    assert report is not None
    assert report["llm_summary"]["total_calls"] == 2
    assert report["llm_summary"]["total_input_tokens"] == 540
    assert report["llm_summary"]["total_output_tokens"] == 120
    assert report["llm_summary"]["errors"] == 1
    assert report["llm_summary"]["retries"] == 1
    assert report["llm_summary"]["by_purpose"] == {
        "chunk_metadata": 1, "enrichment.images": 1,
    }


# ---- Enrichment progress -----------------------------------------


def test_enrichment_progress_terminal_status(
    ctx, workspace, artifact_registry,
):
    rec = _make_recorder(workspace, artifact_registry)
    rec.record_enrichment_progress(
        ctx=ctx, run_id="r-1", planned=24,
    )
    rec.record_enrichment_progress(
        ctx=ctx, run_id="r-1", completed=22, skipped=1, failed=1,
        status="PARTIAL_ENRICHMENT",
    )
    report = rec.build_report("r-1")
    assert report["enrichment_summary"] == {
        "planned": 24, "completed": 22, "skipped": 1, "failed": 1,
        "status": "PARTIAL_ENRICHMENT", "detail": None,
    }


# ---- Report writer -----------------------------------------------


def test_write_report_emits_artifact_and_clears_run(
    ctx, workspace, artifact_registry,
):
    audit = _SpyAudit()
    rec = _make_recorder(workspace, artifact_registry, audit=audit)

    with rec.stage(ctx=ctx, run_id="r-1", stage_name="parse"):
        pass
    rec.record_llm_call(
        ctx=ctx, run_id="r-1", stage="compile", purpose="chunk_metadata",
        provider="openai", model="gemini-2.5-pro",
        duration_ms=100, input_tokens=10, output_tokens=5,
    )

    artifact_id = rec.write_report(
        ctx=ctx, run_id="r-1", document_id="doc-1", filename="rfp.pdf",
    )
    assert artifact_id is not None

    # Artifact exists in the registry and on disk.
    record = artifact_registry.get(ctx, artifact_id)
    assert record.kind == ARTIFACT_KIND_DIAGNOSTIC_REPORT
    on_disk = workspace.project_root(ctx) / record.location
    body = json.loads(on_disk.read_bytes())
    assert body["run_id"] == "r-1"
    assert body["document_id"] == "doc-1"
    assert body["filename"] == "rfp.pdf"
    assert any(s["name"] == "parse" for s in body["stages"])
    assert body["llm_summary"]["total_calls"] == 1
    assert "bottleneck_candidates" in body

    # ``j1.ingestion.diagnostic_report.written`` was emitted.
    written = [e for e in audit.events if e["action"] == EVENT_REPORT_WRITTEN]
    assert len(written) == 1
    assert written[0]["payload"]["artifact_id"] == artifact_id

    # Second write call returns None (run was cleared).
    second = rec.write_report(ctx=ctx, run_id="r-1")
    assert second is None


def test_write_report_returns_none_when_nothing_captured(
    ctx, workspace, artifact_registry,
):
    rec = _make_recorder(workspace, artifact_registry)
    # No stages, no LLM calls — nothing to report.
    assert rec.write_report(ctx=ctx, run_id="r-empty") is None


def test_bottleneck_candidates_are_top_stages_by_duration(
    ctx, workspace, artifact_registry,
):
    rec = _make_recorder(workspace, artifact_registry)
    rec.record_stage_event(
        ctx=ctx, run_id="r-1", stage_name="parse",
        duration_ms=480_000, counters={},
    )
    rec.record_stage_event(
        ctx=ctx, run_id="r-1", stage_name="llm_chunk_metadata",
        duration_ms=720_000, counters={},
    )
    rec.record_stage_event(
        ctx=ctx, run_id="r-1", stage_name="indexing",
        duration_ms=2_100, counters={},
    )
    report = rec.build_report("r-1")
    bottlenecks = report["bottleneck_candidates"]
    assert [b["stage"] for b in bottlenecks] == [
        "llm_chunk_metadata", "parse", "indexing",
    ]
    # Share rounds to 3 dp; sum is approximately 1 across all
    # observed stages.
    assert abs(sum(b["share"] for b in bottlenecks) - 1.0) < 0.005


# ---- Robustness --------------------------------------------------


def test_limiter_emits_llm_call_when_run_context_is_set(
    ctx, workspace, artifact_registry,
):
    """End-to-end attribution check: a function run under the
    limiter while the contextvar holds a ``RunContext`` lands a
    ``llm_call`` event on the recorder — no call-site change needed."""
    from j1.processing.diagnostics import (
        RunContext, set_current_run_context,
    )
    from j1.processing.llm_call_limiter import LLMCallLimiter

    rec = _make_recorder(workspace, artifact_registry)
    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=5, retry_limit=0,
    )

    class _FakeLLMClient:
        provider = "openai_compat"
        model = "gemini-2.5-pro"

        def extract(self, prompt: str) -> str:
            return f"answer({prompt})"

    client = _FakeLLMClient()
    rc = RunContext(
        run_id="r-1", document_id="doc-1", stage="compile",
        recorder=rec, ctx=ctx,
    )
    with set_current_run_context(rc):
        out, stats = limiter.run_with_stats(
            client.extract, "hello", purpose="chunk_metadata",
        )
    assert out == "answer(hello)"
    assert stats.attempts == 1

    report = rec.build_report("r-1")
    assert report is not None
    assert report["llm_summary"]["total_calls"] == 1
    only_call = report["llm_calls"][0]
    assert only_call["purpose"] == "chunk_metadata"
    assert only_call["provider"] == "openai_compat"
    assert only_call["model"] == "gemini-2.5-pro"
    assert only_call["error"] is None


def test_limiter_records_error_on_failure(
    ctx, workspace, artifact_registry,
):
    """The error branch reports the FAILED call (one event) so the
    aggregate counters reflect both successful and failed calls."""
    from j1.processing.diagnostics import (
        RunContext, set_current_run_context,
    )
    from j1.processing.llm_call_limiter import LLMCallLimiter

    rec = _make_recorder(workspace, artifact_registry)
    limiter = LLMCallLimiter(
        max_concurrency=1, timeout_seconds=5, retry_limit=0,
    )

    def _broken():
        raise RuntimeError("backend down")

    rc = RunContext(
        run_id="r-1", document_id="doc-1", stage="enrich",
        recorder=rec, ctx=ctx,
    )
    with set_current_run_context(rc):
        with pytest.raises(RuntimeError):
            limiter.run_with_stats(_broken, purpose="enrichment.images")

    report = rec.build_report("r-1")
    assert report is not None
    assert report["llm_summary"]["total_calls"] == 1
    assert report["llm_summary"]["errors"] == 1
    assert report["llm_calls"][0]["error"] == "RuntimeError"


def test_audit_failure_does_not_break_stage_wrap(
    ctx, workspace, artifact_registry,
):
    """When the audit emit itself raises, the stage wrap MUST
    still complete cleanly — diagnostic infra never breaks the
    activity it's instrumenting."""

    class _ExplodingAudit:
        def record(self, *args, **kwargs):
            raise RuntimeError("audit sink is down")

    rec = _make_recorder(workspace, artifact_registry, audit=_ExplodingAudit())
    # No exception should escape.
    with rec.stage(ctx=ctx, run_id="r-1", stage_name="compile"):
        pass
    report = rec.build_report("r-1")
    assert report is not None
    assert any(s["name"] == "compile" for s in report["stages"])
