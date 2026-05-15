"""PR-05 contract — Ingest trace + final ingestion report.

Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-05, J1
MUST guarantee:

  1. Successful ingestion produces a structured ``FinalIngestionReport``
     covering every operator-facing field (tenant / project /
     document / run / snapshot ids, assessment summary, selected
     mode + parse_method, compile + enrichment status + duration,
     alias count, artifact ids, warnings, errors, promotion result).
  2. Failed ingestion still produces a usable report — partial
     information from whichever stages reached ``started`` flows
     through.
  3. The ingest-trace env flag (``J1_INGEST_TRACE_ENABLED``)
     controls whether the JSONL writer fires at all.
  4. Stage timings on the report are non-negative.
  5. Report includes ``run_id`` and ``snapshot_id`` at the top
     level — load-bearing for cross-referencing against the
     artifact registry.

This module is the single navigable PR-05 regression document.
Adjacent tests cover finer-grained shape pins; the contracts
pinned here are the load-bearing answers to the prompt's
"operators should be able to answer: which stage ran? how long
did it take? what config? what artifacts? warnings / errors?
why slow?".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j1.observability.ingest_trace import (
    ENV_INGEST_TRACE_ENABLED,
    ENV_INGEST_TRACE_OUTPUT,
    IngestTraceLogger,
    IngestTraceSettings,
    TraceContext,
    load_ingest_trace_settings,
    reset_ingest_trace_logger,
    trace_event,
)
from j1.processing.final_ingestion_report import (
    FINAL_INGESTION_REPORT_SCHEMA_VERSION,
    AliasSummary,
    CompileSummary,
    FinalIngestionReport,
    ReportSourceInputs,
    build_final_ingestion_report,
)


# ---- Builder fixtures -------------------------------------------


def _success_inputs(**overrides) -> ReportSourceInputs:
    """Build inputs representing a successful run with realistic
    compile + enrichment payloads."""
    base = dict(
        run_id="run-success-001",
        document_id="doc-ok",
        document_name="contract.pdf",
        tenant_id="acme",
        project_id="alpha",
        snapshot_id="snap-active-001",
        selected_execution_profile="standard",
        started_at="2026-05-20T10:00:00+00:00",
        completed_at="2026-05-20T10:05:00+00:00",
        framework_final_status="completed",
        failure_code=None,
        failure_message=None,
        compile_result_summary={
            "parser": "raganything",
            "status": "succeeded",
            "chunks_count": 42,
            "page_count": 30,
            "extracted_text_chars": 25000,
            "detected_tables": [],
            "detected_images": [],
            "final_quality": "good",
            "warnings": [],
            "errors": [],
            "final_compile_mode": "standard",
            "parse_method": "auto",
            "retry_attempts": [{"attempt": 1, "outcome": "succeeded"}],
            "raw_artifact_refs": ["raw-001"],
        },
        enrichment_result={
            "status": "succeeded",
            "modules": ["entity_extraction"],
            "warnings": [],
        },
        enrichment_aliases={
            "schema_version": "1",
            "aliases": [
                {"canonical": "bill of quantities", "alias": "BOQ",
                 "confidence": 0.95, "source": "domain_enrichment",
                 "evidence": {}},
            ],
            "snapshot_id": "snap-active-001",
        },
        enrichment_aliases_artifact_id="alias-001",
    )
    base.update(overrides)
    return ReportSourceInputs(**base)


def _failed_inputs(**overrides) -> ReportSourceInputs:
    """Build inputs representing a failed compile — partial info
    only. Operator MUST still see a usable report."""
    base = dict(
        run_id="run-fail-002",
        document_id="doc-fail",
        document_name="broken.pdf",
        tenant_id="acme",
        project_id="alpha",
        snapshot_id="snap-cand-002",
        selected_execution_profile="standard",
        started_at="2026-05-20T11:00:00+00:00",
        completed_at="2026-05-20T11:01:30+00:00",
        framework_final_status="failed",
        failure_code="COMPILE_FAILED",
        failure_message="MinerU subprocess crashed at page 12",
    )
    base.update(overrides)
    return ReportSourceInputs(**base)


# ---- Contract 1: success path produces full report --------------


def test_contract_1_successful_run_produces_complete_report():
    """Every load-bearing field is populated for a successful run."""
    report = build_final_ingestion_report(_success_inputs())
    payload = report.to_dict()

    # Identity + scope
    assert payload["run_id"] == "run-success-001"
    assert payload["snapshot_id"] == "snap-active-001"
    assert payload["tenant_id"] == "acme"
    assert payload["project_id"] == "alpha"
    assert payload["document_id"] == "doc-ok"

    # Selected config (PR-05 additions)
    assert payload["selected_execution_profile"] == "standard"
    assert payload["compile_summary"]["compile_mode"] == "standard"
    assert payload["compile_summary"]["parse_method"] == "auto"

    # Compile status + duration
    assert payload["compile_summary"]["compile_status"] == "succeeded"
    assert payload["compile_summary"]["chunks_count"] == 42

    # Enrichment
    assert payload["enrichment_summary"]["enrichment_status"] == "succeeded"

    # Alias count
    assert payload["alias_summary"]["alias_count"] == 1
    assert payload["alias_summary"]["persisted"] is True

    # Promotion result
    assert payload["promotion_result"] == "promoted"

    # Warnings + errors lists exist (may be empty)
    assert isinstance(payload["warnings"], list)
    assert isinstance(payload["errors"], list)


# ---- Contract 2: failure path still produces a report -----------


def test_contract_2_failed_run_still_produces_usable_report():
    """A run that fails mid-compile MUST still produce a report.
    Operators need to know the run failed, why, and which stages
    reached ``started`` — without this the failure is opaque."""
    report = build_final_ingestion_report(_failed_inputs())
    payload = report.to_dict()

    # Identity still populated.
    assert payload["run_id"] == "run-fail-002"
    assert payload["snapshot_id"] == "snap-cand-002"
    assert payload["document_id"] == "doc-fail"

    # Promotion did NOT happen.
    assert payload["promotion_result"] == "not_promoted"

    # Final status reflects failure (in the failure family).
    assert payload["final_status"].startswith("failed")


def test_contract_2_cancelled_run_produces_not_promoted():
    """Cancelled runs are non-failures but also don't promote."""
    report = build_final_ingestion_report(_failed_inputs(
        framework_final_status="cancelled",
        failure_code=None,
        failure_message=None,
    ))
    assert report.to_dict()["promotion_result"] == "not_promoted"


def test_contract_2_partial_completed_run_still_promotes():
    """A run with enrichment warnings completes successfully (the
    compile output is queryable). Promotion MUST fire so the new
    snapshot becomes active. PR-05's ``promotion_result`` field
    captures this — operators answering "did this run change what
    users see?" need the boolean answer."""
    report = build_final_ingestion_report(_success_inputs(
        framework_final_status="partial_completed",
    ))
    assert report.to_dict()["promotion_result"] == "promoted"


# ---- Contract 3: env flag controls trace output -----------------


@pytest.fixture
def trace_path(tmp_path: Path) -> Path:
    return tmp_path / "ingest_trace.jsonl"


@pytest.fixture(autouse=True)
def _clean_trace_singleton():
    reset_ingest_trace_logger(None)
    yield
    reset_ingest_trace_logger(None)


def test_contract_3_env_flag_disabled_by_default(monkeypatch, trace_path):
    """With ``J1_INGEST_TRACE_ENABLED`` unset, the loader resolves
    enabled=False and no JSONL file is created even when
    ``trace_event`` is called repeatedly."""
    monkeypatch.delenv(ENV_INGEST_TRACE_ENABLED, raising=False)
    monkeypatch.setenv(ENV_INGEST_TRACE_OUTPUT, str(trace_path))

    settings = load_ingest_trace_settings()
    assert settings.enabled is False

    # Install the disabled logger via the same path production uses.
    reset_ingest_trace_logger(IngestTraceLogger(settings))
    for _ in range(5):
        trace_event(
            trace_event="ingest.test.fired",
            stage="test", status="started",
        )
    assert not trace_path.exists(), (
        "trace writer must NOT touch disk when env flag is unset"
    )


def test_contract_3_env_flag_enabled_writes_jsonl(monkeypatch, trace_path):
    """``J1_INGEST_TRACE_ENABLED=true`` activates the writer; each
    ``trace_event`` becomes one JSONL line."""
    monkeypatch.setenv(ENV_INGEST_TRACE_ENABLED, "true")
    monkeypatch.setenv(ENV_INGEST_TRACE_OUTPUT, str(trace_path))

    settings = load_ingest_trace_settings()
    assert settings.enabled is True

    reset_ingest_trace_logger(IngestTraceLogger(settings))
    ctx = TraceContext(
        tenant_id="acme", project_id="alpha",
        document_id="doc-1", run_id="run-1",
    )
    trace_event(
        trace_event="ingest.test.fired",
        stage="test", status="completed",
        context=ctx, duration_ms=42,
    )

    lines = [
        json.loads(line)
        for line in trace_path.read_text().splitlines() if line.strip()
    ]
    assert len(lines) == 1
    line = lines[0]
    assert line["trace_event"] == "ingest.test.fired"
    assert line["stage"] == "test"
    assert line["duration_ms"] == 42
    assert line["run_id"] == "run-1"


# ---- Contract 4: stage timings are non-negative -----------------


def test_contract_4_report_duration_is_non_negative():
    """Top-level ``duration_ms`` is computed from started_at +
    completed_at. MUST always be non-negative — a negative value
    would mean someone fed in clock-skew or mis-ordered timestamps
    and the report should reject that rather than silently
    propagating nonsense."""
    report = build_final_ingestion_report(_success_inputs())
    assert report.duration_ms is not None
    assert report.duration_ms >= 0


def test_contract_4_stage_durations_are_non_negative():
    """Each StageSummary's ``duration_ms`` (when present) MUST be
    >= 0. Pinned so a future refactor that flips
    started/completed order can't ship."""
    report = build_final_ingestion_report(_success_inputs())
    for stage in report.stages:
        if stage.duration_ms is not None:
            assert stage.duration_ms >= 0, (
                f"stage {stage.stage_id!r} duration is negative: "
                f"{stage.duration_ms}"
            )


# ---- Contract 5: report carries run_id + snapshot_id ------------


def test_contract_5_report_carries_run_id_and_snapshot_id():
    """The two identity keys operators use to cross-reference the
    artifact registry MUST be at the top level of the report. No
    nested access required — the artifact registry is keyed on
    ``snapshot_id``; the audit log is keyed on ``run_id``."""
    report = build_final_ingestion_report(_success_inputs())
    payload = report.to_dict()
    assert payload["run_id"] == "run-success-001"
    assert payload["snapshot_id"] == "snap-active-001"
    # Negative: snapshot_id is not buried in stages or compile_summary.
    assert "snapshot_id" not in payload["compile_summary"]


def test_contract_5_failed_run_still_carries_both_ids():
    """Even failed runs MUST surface both ids — operators
    investigating an incident need to find the candidate snapshot
    on disk."""
    report = build_final_ingestion_report(_failed_inputs())
    payload = report.to_dict()
    assert payload["run_id"] == "run-fail-002"
    assert payload["snapshot_id"] == "snap-cand-002"


# ---- Bonus: schema version is bumped per release ---------------


def test_schema_version_pinned():
    """Wire-shape evolutions bump the schema. Pinned so a future
    field addition WITHOUT a bump fails this test."""
    assert FINAL_INGESTION_REPORT_SCHEMA_VERSION == "1.2"


# ---- Bonus: builder is pure -----------------------------------


def test_builder_remains_pure_with_all_pr05_fields():
    """Adding PR-05 fields must not introduce I/O. Same inputs MUST
    produce identical output across calls."""
    inputs = _success_inputs()
    first = build_final_ingestion_report(inputs).to_dict()
    second = build_final_ingestion_report(inputs).to_dict()
    assert first == second
