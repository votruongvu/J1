"""End-to-end demo of the Phase-1 diagnostic recorder.

Drives the REAL ``DiagnosticRecorder`` + REAL ``LLMCallLimiter``
through a simulated 89-page RFP ingestion. The numbers below
(``parse_elapsed_ms``, chunk LLM call durations, enrichment item
mix) are seeded against the symptoms in the user's report, NOT
measured against a real document — the point is to exercise the
wiring and produce a representative report you can read against
your real one when it lands.

What's genuinely exercised:

  * ``recorder.stage(...)`` context manager + audit-event emit
  * ``RunContext`` propagation via ``contextvars`` so LLM calls
    invoked under the limiter get attributed to the active stage
  * ``LLMCallLimiter.run_with_stats(..., purpose=...)`` end-to-end
  * ``recorder.write_report(...)`` artifact write
  * Bottleneck-candidate computation

What's seeded:

  * Stage durations (sleep + fake processor returns)
  * Chunk LLM call counts (412 calls, each ~1.8s)
  * Enrichment item mix (planned=24, completed=22, skipped=1, failed=1)
  * Token estimates per call (~420 in / 80 out)

Run with: python -m scripts.demo_diagnostic_report
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from j1.processing.diagnostics import (
    DiagnosticRecorder,
    RunContext,
    set_current_run_context,
)
from j1.processing.llm_call_limiter import LLMCallLimiter


# ---- Stub collaborators ------------------------------------------


class _CapturingAudit:
    """Mimics ``DefaultAuditRecorder`` — just enough surface for the
    recorder to call ``record(...)``. The demo prints a summary of
    captured events at the end so you can see WHICH stable event
    names fired."""

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


class _StubContext:
    """A minimal ``ProjectContext``-like value for the demo (the
    recorder only reads ``tenant_id`` + ``project_id`` defensively
    via getattr; the demo audit doesn't care)."""

    tenant_id = "acme"
    project_id = "alpha"


class _FakeLLMClient:
    """Mimics the bound-method shape ``getattr(fn, '__self__')``
    returns from a real LLM client. The limiter reads
    ``provider`` + ``model`` off this to populate the report."""

    provider = "openai_compat"
    model = "gemini-2.5-pro"

    def chunk_metadata_call(self, *, prompt_len: int) -> dict:
        # Simulate a real per-chunk LLM call. ~1.8s mirrors the
        # observed gemini-2.5-pro structured-output latency for
        # chunk-metadata prompts at this token budget.
        time.sleep(0.018)  # 18ms scaled-down for the demo (real ≈ 1800ms)
        return {"input_tokens": 420, "output_tokens": 80}


# ---- Simulation --------------------------------------------------


def _simulate_89_page_rfp(rec: DiagnosticRecorder, ctx) -> str:
    """Walk through the activity layer the way a real run would.

    Returns the run_id so the caller can build the report.
    """
    run_id = "demo-run-89p-rfp"
    document_id = "demo-doc-89p-rfp"
    rec.set_metadata(
        run_id=run_id, document_id=document_id,
        filename="rfp-89p.pdf",
    )

    # ---- Assessment stage ------------------------------------
    # Production: ``build_initial_execution_plan`` activity.
    # Seeded as instantaneous (<10ms) — the planner is cheap and
    # bottleneck rankings should show it last.
    with rec.stage(
        ctx=ctx, run_id=run_id, document_id=document_id,
        stage_name="assessment", counters={"signals_evaluated": 7},
    ) as st:
        time.sleep(0.002)
        st.update(mode_selected_deep=1)

    # ---- Compile + parse -------------------------------------
    # Production: the compile activity wraps its body in
    # ``_diag_stage_wrap``; the bridge surfaces parse_elapsed_ms.
    # We emit them as two sibling stages — same pattern the patched
    # compile activity uses.
    with rec.stage(
        ctx=ctx, run_id=run_id, document_id=document_id,
        stage_name="compile", counters={"page_count": 89},
    ) as st:
        # MinerU/raganything parse — the largest single block on
        # the 89-page RFP. ~8 minutes wall-clock = 480_000 ms.
        rec.record_stage_event(
            ctx=ctx, run_id=run_id, document_id=document_id,
            stage_name="parse",
            duration_ms=480_000,
            counters={
                "page_count": 89,
                "extracted_text_chars": 312_440,
                "image_count": 3,
                "table_count": 11,
            },
        )
        # The compile-level wrap measures the activity body — for
        # the demo, the parse dominates so we approximate the
        # body with the same number plus a small overhead for
        # the bridge's manifest build.
        time.sleep(0.005)
        st.update(
            chunk_count=412,
            extracted_text_chars=312_440,
            page_count=89,
            image_count=3,
            table_count=11,
            artifact_count=413,  # 412 chunks + 1 manifest
        )

    # ---- Chunk-metadata LLM calls ----------------------------
    # 412 chunks each going through the limiter for an
    # LLM-backed metadata pass. This is the second-biggest
    # bottleneck observed in the user's 23-min run.
    limiter = LLMCallLimiter(
        max_concurrency=4, timeout_seconds=120, retry_limit=1,
    )
    client = _FakeLLMClient()
    with rec.stage(
        ctx=ctx, run_id=run_id, document_id=document_id,
        stage_name="llm_chunk_metadata",
        counters={"planned": 412},
    ) as st:
        rc = RunContext(
            run_id=run_id, document_id=document_id,
            stage="llm_chunk_metadata", recorder=rec, ctx=ctx,
        )
        succeeded = 0
        retried = 0
        with set_current_run_context(rc):
            # Demo: drive 412 calls but use 50 in the script so
            # the demo finishes in a reasonable time. Scale the
            # report's totals by 412/50 at the end.
            sample_n = 50
            for i in range(sample_n):
                _, stats = limiter.run_with_stats(
                    client.chunk_metadata_call,
                    prompt_len=420,
                    purpose="chunk_metadata",
                )
                succeeded += 1
                if stats.retried:
                    retried += 1
        st.update(
            attempted=sample_n,
            succeeded=succeeded,
            retried=retried,
        )

    # ---- Enrichment ------------------------------------------
    with rec.stage(
        ctx=ctx, run_id=run_id, document_id=document_id,
        stage_name="enrichment",
        counters={"planned": 24},
    ) as st:
        rec.record_enrichment_progress(
            ctx=ctx, run_id=run_id, document_id=document_id,
            planned=24,
        )
        # 22 succeed, 1 skipped (no client), 1 failed (timeout).
        rc = RunContext(
            run_id=run_id, document_id=document_id,
            stage="enrichment", recorder=rec, ctx=ctx,
        )
        with set_current_run_context(rc):
            for label in ("images", "tables", "summaries"):
                limiter.run_with_stats(
                    client.chunk_metadata_call, prompt_len=600,
                    purpose=f"enrichment.{label}",
                )
        rec.record_enrichment_progress(
            ctx=ctx, run_id=run_id, document_id=document_id,
            completed=22, skipped=1, failed=1,
            status="PARTIAL_ENRICHMENT",
            detail="1 image enricher timed out",
        )
        st.update(completed=22, skipped=1, failed=1)

    # ---- Graph build + index --------------------------------
    with rec.stage(
        ctx=ctx, run_id=run_id, document_id=document_id,
        stage_name="build_graph", counters={"input_artifact_count": 412},
    ) as st:
        time.sleep(0.008)
        st.update(nodes=312, edges=587)

    with rec.stage(
        ctx=ctx, run_id=run_id, document_id=document_id,
        stage_name="index", counters={"input_artifact_count": 412},
    ) as st:
        time.sleep(0.002)
        st.update(rows_inserted=412)

    return run_id


# ---- Entry point -------------------------------------------------


def main() -> int:
    out_dir = Path(__file__).resolve().parents[1] / "build" / "diag-demo"
    out_dir.mkdir(parents=True, exist_ok=True)

    audit = _CapturingAudit()
    rec = DiagnosticRecorder(
        audit=audit,
        # No artifact registry / workspace wired — the demo skips
        # the on-disk artifact write and falls back to printing
        # the report inline. Production wiring (deploy/dev/_wiring)
        # injects all three.
        artifact_registry=None,
        workspace=None,
    )
    ctx = _StubContext()

    run_id = _simulate_89_page_rfp(rec, ctx)

    report = rec.build_report(run_id)
    if report is None:
        print("ERROR: build_report returned None — recorder didn't capture")
        return 1

    # Write to disk so a tester can `jq` it
    out_path = out_dir / "ingestion_diagnostic_report.demo.json"
    out_path.write_text(json.dumps(report, indent=2, sort_keys=False))
    print(f"\n=== Report written to: {out_path}\n")

    _print_summary(report, audit)
    return 0


def _print_summary(report: dict, audit) -> None:
    """Pretty-print the report's headline numbers + the audit
    event timeline. This is the same shape the
    ``scripts/print_diagnostic_report.py`` follow-up will use."""
    print(f"run_id         : {report['run_id']}")
    print(f"document_id    : {report['document_id']}")
    print(f"filename       : {report['filename']}")
    print()
    print("--- Stages -----------------------------------------------")
    print(f"{'stage':<22} {'duration_ms':>12}  {'success':>8}  counters")
    for s in report["stages"]:
        dur = s.get("duration_ms")
        success = "yes" if s.get("success") else "NO"
        counter_summary = ", ".join(
            f"{k}={v}" for k, v in s.get("counters", {}).items()
        ) or "—"
        dur_s = f"{dur:>12,}" if isinstance(dur, int) else f"{'?':>12}"
        print(f"{s['name']:<22} {dur_s}  {success:>8}  {counter_summary}")
    print()
    print("--- LLM summary -----------------------------------------")
    ls = report["llm_summary"]
    print(f"total_calls      : {ls['total_calls']:,}")
    print(f"total_input_tok  : {ls['total_input_tokens']:,}")
    print(f"total_output_tok : {ls['total_output_tokens']:,}")
    print(f"total_duration_ms: {ls['total_duration_ms']:,}")
    print(f"errors           : {ls['errors']}")
    print(f"retries          : {ls['retries']}")
    print(f"by_purpose       : {ls['by_purpose']}")
    print()
    print("--- Enrichment ------------------------------------------")
    es = report["enrichment_summary"]
    print(f"status   : {es['status']}")
    print(f"planned  : {es['planned']}")
    print(f"completed: {es['completed']}")
    print(f"skipped  : {es['skipped']}")
    print(f"failed   : {es['failed']}")
    if es.get("detail"):
        print(f"detail   : {es['detail']}")
    print()
    print("--- Top bottleneck candidates ---------------------------")
    for b in report["bottleneck_candidates"]:
        share = b.get("share", 0.0)
        print(
            f"  {b['stage']:<22} {b['duration_ms']:>10,} ms  "
            f"({share * 100:.1f}% of total)"
        )
    print()
    print("--- Audit events emitted --------------------------------")
    counts: dict[str, int] = {}
    for e in audit.events:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
    for action, n in sorted(counts.items()):
        print(f"  {action:<46} {n:>4}")
    print()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
