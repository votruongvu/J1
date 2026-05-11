"""tests for the typed `FinalIngestionReport` builder.

Pins:
 1. Per-(A–F) projection: each final status produces a
 report carrying the right stage statuses + summaries.
 2. Serialization round-trip stability via `to_dict`.
 3. Artifact-ref pluck + retry-count + raw-compile-ref surfacing.
 4. Zero stale vocabulary (split_mode / gating / IngestPlanner).
"""

from __future__ import annotations

import inspect

import pytest

from j1.processing.final_ingestion_report import (
    FINAL_INGESTION_REPORT_SCHEMA_VERSION,
    REQUIRED_STAGE_IDS,
    STAGE_ID_ASSESSMENT,
    STAGE_ID_COMPILE,
    STAGE_ID_COMPILE_RESULT_NORMALIZATION,
    STAGE_ID_ENRICHMENT,
    STAGE_ID_FINALIZATION,
    STAGE_ID_POST_COMPILE_ANALYSIS,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_SKIPPED,
    STAGE_STATUS_SUCCEEDED,
    STAGE_STATUS_SUCCEEDED_WITH_WARNINGS,
    ReportSourceInputs,
    build_final_ingestion_report,
)
from j1.processing.final_status import (
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
    INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
    INGESTION_STATUS_FAILED_COMPILE,
    INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
    INGESTION_STATUS_FAILED_FINALIZATION,
)


# ---- Fixtures / factories ------------------------------------------


def _initial_plan(domain: str = "civil_engineering") -> dict:
    return {
        "schema_version": "1",
        "document_id": "doc-1",
        "domain_profile_id": domain,
        "enrichment_policy": "auto",
        "require_enrichment_success": False,
        "candidate_modules": ["metadata_enrichment"],
        "warnings": [],
    }


def _compile_result(
    *, chunks: int = 42, retries: int = 0, quality: str = "good",
    warnings: list[str] | None = None,
) -> dict:
    attempts = [
        {"attempt_number": i + 1, "status": "succeeded"}
        for i in range(retries + 1)
    ]
    return {
        "schema_version": "1",
        "document_id": "doc-1",
        "parser": "mineru",
        "parse_method": "auto",
        "status": "succeeded",
        "chunks_count": chunks,
        "extracted_text_chars": 15_000,
        "page_count": 10,
        "detected_tables": [{}, {}],
        "detected_images": [{}],
        "final_quality": quality,
        "retry_attempts": attempts,
        "raw_artifact_refs": ["raw-1", "raw-2"],
        "warnings": warnings or [],
        "errors": [],
    }


def _enrich_plan(
    *, should_enrich: bool = True, require_success: bool = False,
    reasons: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "1",
        "overall_recommendation": "recommended" if should_enrich else "skip",
        "reasons": reasons or [],
        "recommended_tasks": ["metadata_enrichment"] if should_enrich else [],
        "skipped_tasks": [],
        "blocking_issues": [],
        "source_signals": {},
        "decision_source": "rule_based",
        "should_enrich": should_enrich,
        "require_enrichment_success": require_success,
    }


def _enrichment_result(
    *, status: str = "succeeded",
    reason: str | None = None,
    metadata_fields: int = 0,
    terminology_count: int = 0,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "1",
        "document_id": "doc-1",
        "status": status,
        "reason": reason or "",
        "domain_id": "civil_engineering",
        "module_outcomes": [
            {"module_id": "metadata_enrichment", "status": "run"},
        ],
        "document_metadata": {f"field_{i}": "x" for i in range(metadata_fields)},
        "terminology": [{} for _ in range(terminology_count)],
        "warnings": warnings or [],
        "errors": errors or [],
    }


def _inputs(**overrides) -> ReportSourceInputs:
    base = dict(
        run_id="run-1",
        document_id="doc-1",
        document_name="spec.pdf",
        tenant_id="acme",
        project_id="alpha",
        started_at="2026-05-11T00:00:00Z",
        completed_at="2026-05-11T00:01:00Z",
        framework_final_status="completed",
        failure_code=None,
        failure_message=None,
        warning_count=0,
        initial_execution_plan=_initial_plan(),
        compile_result_summary=_compile_result(),
        post_compile_enrich_plan=_enrich_plan(),
        enrichment_result=_enrichment_result(),
        final_summary={"final_status": "succeeded"},
        artifact_refs={
            "initial_execution_plan": "art-init-1",
            "compile_result_summary": "art-cmp-1",
            "post_compile_enrich_plan": "art-pcp-1",
            "enrichment_result": "art-enr-1",
            "final_summary": "art-fs-1",
        },
        raw_compile_artifact_refs=("raw-1", "raw-2"),
    )
    base.update(overrides)
    return ReportSourceInputs(**base)


# ---- 1. Round-trip + structure --------------------------------------


def test_report_schema_version_pinned():
    report = build_final_ingestion_report(_inputs())
    assert report.schema_version == FINAL_INGESTION_REPORT_SCHEMA_VERSION


def test_report_has_all_required_stages_in_order():
    report = build_final_ingestion_report(_inputs())
    stage_ids = tuple(s.stage_id for s in report.stages)
    assert stage_ids == REQUIRED_STAGE_IDS


def test_to_dict_round_trip_carries_all_top_level_keys():
    report = build_final_ingestion_report(_inputs())
    d = report.to_dict()
    expected = {
        "schema_version", "run_id", "document_id", "document_name",
        "tenant_id", "project_id", "domain_profile_id",
        "started_at", "completed_at", "duration_ms",
        "final_status", "final_status_reason",
        "stages", "compile_summary", "enrichment_summary",
        "artifact_refs", "warnings", "errors", "retry_counts",
        "operator_notes",
    }
    assert expected <= set(d.keys())


def test_duration_ms_is_computed_from_iso_timestamps():
    report = build_final_ingestion_report(_inputs())
    assert report.duration_ms == 60_000


def test_duration_ms_is_none_when_missing_timestamp():
    report = build_final_ingestion_report(_inputs(completed_at=None))
    assert report.duration_ms is None


# ---- 2. Per-(A–F) projection ----------------------------------------


def test_A_completed_without_enrichment_marks_enrichment_skipped():
    report = build_final_ingestion_report(_inputs(
        enrichment_result=_enrichment_result(
            status="skipped", reason="domain policy=never",
        ),
        post_compile_enrich_plan=_enrich_plan(
            should_enrich=False,
            reasons=["domain policy=never"],
        ),
    ))
    assert report.final_status == INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT
    enrichment_stage = next(
        s for s in report.stages if s.stage_id == STAGE_ID_ENRICHMENT
    )
    assert enrichment_stage.status == STAGE_STATUS_SKIPPED
    assert "domain policy=never" in (
        enrichment_stage.reasons[0] if enrichment_stage.reasons else ""
    )
    assert report.enrichment_summary.skipped_reason == "domain policy=never"


def test_B_completed_with_enrichment_marks_all_success():
    report = build_final_ingestion_report(_inputs(
        enrichment_result=_enrichment_result(
            status="succeeded",
            metadata_fields=3,
            terminology_count=12,
        ),
    ))
    assert report.final_status == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT
    enrichment_stage = next(
        s for s in report.stages if s.stage_id == STAGE_ID_ENRICHMENT
    )
    assert enrichment_stage.status == STAGE_STATUS_SUCCEEDED
    additions = report.enrichment_summary.what_enrichment_added
    assert any("Document metadata" in a for a in additions)
    assert any("Terminology" in a for a in additions)


def test_C_completed_with_warnings_marks_enrichment_warning():
    report = build_final_ingestion_report(_inputs(
        enrichment_result=_enrichment_result(
            status="succeeded_with_warnings",
            warnings=["module-x ran with degraded output"],
        ),
        framework_final_status="partial_completed",
        warning_count=1,
    ))
    assert (
        report.final_status
        == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS
    )
    enrichment_stage = next(
        s for s in report.stages if s.stage_id == STAGE_ID_ENRICHMENT
    )
    assert enrichment_stage.status == STAGE_STATUS_SUCCEEDED_WITH_WARNINGS
    assert "degraded output" in report.warnings[0]


def test_D_failed_compile_marks_downstream_skipped_no_enrichment_pretense():
    report = build_final_ingestion_report(_inputs(
        framework_final_status="failed",
        failure_code="COMPILE_FAILED",
        failure_message="parse_method=auto produced 0 chunks",
        compile_result_summary=None,
        post_compile_enrich_plan=None,
        enrichment_result=None,
        final_summary=None,
        artifact_refs={
            "initial_execution_plan": "art-init-1",
        },
    ))
    assert report.final_status == INGESTION_STATUS_FAILED_COMPILE
    compile_stage = next(
        s for s in report.stages if s.stage_id == STAGE_ID_COMPILE
    )
    assert compile_stage.status == STAGE_STATUS_FAILED
    # Compile-result-normalization, post-compile, enrichment, finalize
    # must not show as SUCCEEDED.
    for stage_id in (
        STAGE_ID_COMPILE_RESULT_NORMALIZATION,
        STAGE_ID_POST_COMPILE_ANALYSIS,
        STAGE_ID_ENRICHMENT,
        STAGE_ID_FINALIZATION,
    ):
        stage = next(s for s in report.stages if s.stage_id == stage_id)
        assert stage.status != STAGE_STATUS_SUCCEEDED


def test_E_failed_enrichment_required_marks_enrichment_failed():
    report = build_final_ingestion_report(_inputs(
        framework_final_status="failed",
        failure_code="ENRICHMENT_REQUIRED",
        failure_message="enrichment required but failed",
        enrichment_result=_enrichment_result(
            status="failed",
            errors=["module-x raised TimeoutError"],
        ),
        post_compile_enrich_plan=_enrich_plan(
            should_enrich=True, require_success=True,
        ),
    ))
    assert report.final_status == INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED
    enrichment_stage = next(
        s for s in report.stages if s.stage_id == STAGE_ID_ENRICHMENT
    )
    assert enrichment_stage.status == STAGE_STATUS_FAILED
    # Compile must still show as SUCCEEDED (the compile output is
    # preserved; it's the post-compile enrichment that failed).
    compile_stage = next(
        s for s in report.stages if s.stage_id == STAGE_ID_COMPILE
    )
    assert compile_stage.status == STAGE_STATUS_SUCCEEDED
    assert report.enrichment_summary.require_enrichment_success is True


def test_F_failed_finalization_keeps_previous_stages_intact():
    report = build_final_ingestion_report(_inputs(
        framework_final_status="failed",
        failure_code="FINALIZATION_FAILED",
        failure_message="finalize raised",
        final_summary=None,
    ))
    assert report.final_status == INGESTION_STATUS_FAILED_FINALIZATION
    finalize = next(
        s for s in report.stages if s.stage_id == STAGE_ID_FINALIZATION
    )
    assert finalize.status == STAGE_STATUS_FAILED
    assert "finalize raised" in finalize.errors
    # Compile + enrichment should still show as SUCCEEDED — they
    # ran cleanly before the finalize failure.
    compile_stage = next(
        s for s in report.stages if s.stage_id == STAGE_ID_COMPILE
    )
    assert compile_stage.status == STAGE_STATUS_SUCCEEDED
    enrichment_stage = next(
        s for s in report.stages if s.stage_id == STAGE_ID_ENRICHMENT
    )
    assert enrichment_stage.status == STAGE_STATUS_SUCCEEDED


# ---- 3. Compile + enrichment summary fields ------------------------


def test_compile_summary_carries_typed_compile_fields():
    report = build_final_ingestion_report(_inputs(
        compile_result_summary=_compile_result(
            chunks=33, retries=1, warnings=["text extracted via OCR"],
        ),
    ))
    cs = report.compile_summary
    assert cs.compile_engine == "mineru"
    assert cs.compile_status == "succeeded"
    assert cs.chunks_count == 33
    assert cs.extracted_text_chars == 15_000
    assert cs.page_count == 10
    assert cs.detected_tables_count == 2
    assert cs.detected_images_count == 1
    assert cs.quality_verdict == "good"
    assert cs.retry_count == 1
    assert "text extracted via OCR" in cs.warnings
    assert "raw-1" in cs.artifact_refs


def test_enrichment_summary_carries_typed_enrichment_fields():
    report = build_final_ingestion_report(_inputs(
        post_compile_enrich_plan=_enrich_plan(
            should_enrich=True, require_success=True,
        ),
        enrichment_result=_enrichment_result(
            status="succeeded",
            metadata_fields=2,
            warnings=["module-y partial"],
        ),
    ))
    es = report.enrichment_summary
    assert es.should_enrich is True
    assert es.enrichment_status == "succeeded"
    assert es.policy == "auto"
    assert es.require_enrichment_success is True
    assert "metadata_enrichment" in es.selected_modules
    assert "module-y partial" in es.warnings


def test_artifact_refs_include_raw_compile_pointers():
    report = build_final_ingestion_report(_inputs())
    assert "raw_compile_artifact_refs" in report.artifact_refs
    # Per-kind ids are preserved.
    assert report.artifact_refs["initial_execution_plan"] == "art-init-1"
    assert report.artifact_refs["enrichment_result"] == "art-enr-1"


def test_retry_counts_carries_compile_and_enrichment():
    report = build_final_ingestion_report(_inputs(
        compile_result_summary=_compile_result(retries=2),
    ))
    assert report.retry_counts == {"compile": 2, "enrichment": 0}


def test_top_level_warnings_aggregate_across_stages():
    report = build_final_ingestion_report(_inputs(
        compile_result_summary=_compile_result(
            warnings=["text extracted via OCR"],
        ),
        enrichment_result=_enrichment_result(
            status="succeeded",
            warnings=["module-x partial"],
        ),
    ))
    assert "text extracted via OCR" in report.warnings
    assert "module-x partial" in report.warnings


# ---- 4. Legacy-vocabulary guard ------------------------------------


def test_report_payload_has_no_split_mode_vocabulary():
    """The report is the operator/FE wire format — must not
 reintroduce split-mode / pre-compile gating / IngestPlanner
 terminology."""
    report = build_final_ingestion_report(_inputs())
    payload = report.to_dict()
    import json
    serialised = json.dumps(payload)
    for forbidden in (
        "split_mode", "SplitMode", "split mode",
        "insert_content",
        "pre_compile_gating", "pre-compile gating",
        "graph gating", "index gating",
        "IngestPlanner",
    ):
        assert forbidden not in serialised, (
            f"forbidden token {forbidden!r} appeared in payload"
        )


def test_report_module_source_has_no_split_mode_vocabulary():
    """Static guard — the source file itself must stay free of the
 legacy vocabulary so docstrings + variable names don't leak it
 via observability paths."""
    from j1.processing import final_ingestion_report
    src = inspect.getsource(final_ingestion_report)
    for forbidden in (
        "split_mode", "SplitMode", "insert_content",
        "pre_compile_gating", "PreCompileGating", "IngestPlanner",
    ):
        assert forbidden not in src


# ---- 5. Empty / minimal-data robustness ----------------------------


def test_minimal_inputs_produce_a_valid_report_without_crashing():
    """Pre- runs (no artifacts persisted yet) should still
 produce a valid report — every stage stays PENDING / SKIPPED."""
    report = build_final_ingestion_report(ReportSourceInputs(
        run_id="run-x",
        document_id=None,
        document_name=None,
        tenant_id=None,
        project_id=None,
        started_at=None,
        completed_at=None,
        framework_final_status="completed",
        failure_code=None,
        failure_message=None,
    ))
    assert len(report.stages) == len(REQUIRED_STAGE_IDS)
    assert report.compile_summary.chunks_count == 0
    assert report.enrichment_summary.enrichment_status is None


def test_skipped_enrichment_reason_falls_back_to_plan_reasons_when_missing_overlay():
    """When the enrichment_result artifact is absent (enrichment
 was filtered out before persistence), the skipped reason can
 fall back to the enrich plan's `reasons[]`."""
    report = build_final_ingestion_report(_inputs(
        enrichment_result=None,
        post_compile_enrich_plan=_enrich_plan(
            should_enrich=False,
            reasons=["assessor: no enrichment-eligible content found"],
        ),
        framework_final_status="completed",
    ))
    assert report.enrichment_summary.skipped_reason is not None
    assert "no enrichment-eligible content" in (
        report.enrichment_summary.skipped_reason or ""
    )


# ---- 6. Stage status vocabulary closure ----------------------------


def test_stage_status_vocabulary_is_pinned():
    """The FE consumes stage statuses verbatim — pin the closed
 vocabulary so adding a new stage status is a coordinated
 change."""
    pinned = {
        STAGE_STATUS_FAILED, STAGE_STATUS_SKIPPED,
        STAGE_STATUS_SUCCEEDED, STAGE_STATUS_SUCCEEDED_WITH_WARNINGS,
    }
    report = build_final_ingestion_report(_inputs())
    for stage in report.stages:
        # Every emitted stage status must be in the pinned set OR
        # the (pending/running) early states — the test pins the
        # closed surface explicitly.
        assert stage.status in (
            pinned | {"pending", "running"}
        )
