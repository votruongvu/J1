"""Unit tests for the Wave 4 typed normalized compile result.

The adapter projects `ArtifactActivityResult.content_stats` +
`compile_metrics` (untyped dicts) onto typed sub-records. These
tests pin:

  * the projection table (each content_stats / compile_metrics key
    maps onto the right typed field);
  * `detected_tables` / `detected_images` projection (placeholder
    behaviour when bridge only surfaces a count; descriptor pass-
    through when bridge surfaces per-element dicts);
  * `quality_signals` aggregation;
  * `retry_history` deserialization from the workflow's per-
    attempt audit dicts;
  * round-trip via `to_payload` / `from_payload`;
  * pure (no LLM / OCR / vendor imports).
"""

from __future__ import annotations

import ast
import inspect
import sys

import pytest

from j1.orchestration.activities.payloads import ArtifactActivityResult
from j1.processing.compile_result import (
    COMPILE_ENGINE_RAGANYTHING,
    CompileAttemptRecord,
    CompileQualitySignals,
    DetectedImage,
    DetectedTable,
    NormalizedCompileResult,
    build_compile_attempt_records,
    normalize_compile_result,
)


def _activity_result(
    *,
    content_stats: dict | None = None,
    compile_metrics: dict | None = None,
    artifact_ids: list[str] | None = None,
    kinds: tuple[str, ...] = (),
    status: str = "succeeded",
    error: str | None = None,
) -> ArtifactActivityResult:
    return ArtifactActivityResult(
        status=status,
        artifact_ids=artifact_ids or [],
        kinds=kinds,
        content_stats=content_stats,
        compile_metrics=compile_metrics or {},
        error=error,
    )


# ---- Compile engine + status ----------------------------------------


def test_default_engine_is_raganything():
    result = normalize_compile_result(
        _activity_result(), document_id="doc-1",
    )
    assert result.compile_engine == COMPILE_ENGINE_RAGANYTHING == "raganything"
    assert result.status == "succeeded"
    assert result.document_id == "doc-1"


def test_engine_swap_is_a_caller_kwarg():
    """Future engine swap = single arg, not a code change."""
    result = normalize_compile_result(
        _activity_result(),
        document_id="doc-1",
        compile_engine="custom_compiler",
        engine_version="2.4.1",
    )
    assert result.compile_engine == "custom_compiler"
    assert result.engine_version == "2.4.1"


# ---- Counts ---------------------------------------------------------


def test_chunks_count_comes_from_compile_metrics_first():
    result = normalize_compile_result(
        _activity_result(
            compile_metrics={"chunks_count": 7, "extracted_text_chars": 1234},
            kinds=("chunk", "chunk"),  # would be 2 from kinds
        ),
        document_id="doc-1",
    )
    assert result.chunks_count == 7  # metrics win over kinds count
    assert result.extracted_text_chars == 1234


def test_chunks_count_falls_back_to_kinds_count():
    """When metrics don't surface chunks_count, count `chunk` kinds."""
    result = normalize_compile_result(
        _activity_result(kinds=("chunk", "chunk", "chunk", "parsed_source")),
        document_id="doc-1",
    )
    assert result.chunks_count == 3


def test_extracted_text_chars_falls_back_to_content_stats():
    result = normalize_compile_result(
        _activity_result(content_stats={"total_text_chars": 8000}),
        document_id="doc-1",
    )
    assert result.extracted_text_chars == 8000


def test_page_count_and_text_block_count_projected():
    result = normalize_compile_result(
        _activity_result(content_stats={
            "page_count": 12,
            "text_block_count": 47,
        }),
        document_id="doc-1",
    )
    assert result.page_count == 12
    assert result.text_block_count == 47


# ---- detected_images ------------------------------------------------


def test_detected_images_projects_descriptor_dicts():
    result = normalize_compile_result(
        _activity_result(content_stats={
            "image_count": 2,
            "images": [
                {"image_id": "img-1", "page": 3, "role": "diagram",
                 "decision": "vision_required", "caption": "fig 1",
                 "score": 0.87},
                {"image_id": "img-2", "page_idx": 7, "role": "chart"},
            ],
        }),
        document_id="doc-1",
    )
    assert len(result.detected_images) == 2
    first = result.detected_images[0]
    assert first == DetectedImage(
        image_id="img-1", page=3, role="diagram",
        decision="vision_required", caption="fig 1", score=0.87,
    )
    second = result.detected_images[1]
    assert second.image_id == "img-2"
    assert second.page == 7  # page_idx aliased to page


def test_detected_images_empty_when_no_descriptors():
    """A compile with image_count > 0 but no per-image descriptors
    yields an empty list — the typed surface intentionally doesn't
    fabricate page/role info."""
    result = normalize_compile_result(
        _activity_result(content_stats={"image_count": 3}),
        document_id="doc-1",
    )
    assert result.detected_images == ()


# ---- detected_tables -----------------------------------------------


def test_detected_tables_uses_descriptors_when_available():
    """Future parsers can surface per-table dicts via
    `content_stats["tables"]`; the normalizer projects them."""
    result = normalize_compile_result(
        _activity_result(content_stats={
            "tables": [
                {"table_id": "t-1", "page": 4, "caption": "Spec table",
                 "row_count": 12, "column_count": 5},
            ],
        }),
        document_id="doc-1",
    )
    assert result.detected_tables == (
        DetectedTable(
            table_id="t-1", page=4, caption="Spec table",
            row_count=12, column_count=5,
        ),
    )


def test_detected_tables_falls_back_to_placeholders_from_count():
    """When the bridge only surfaces a `table_count` (current
    RAGAnything behaviour), the normalizer emits one placeholder
    per count so the FE can render an N-tables tile."""
    result = normalize_compile_result(
        _activity_result(content_stats={"table_count": 3}),
        document_id="doc-1",
    )
    assert len(result.detected_tables) == 3
    assert [t.table_id for t in result.detected_tables] == [
        "table-1", "table-2", "table-3",
    ]
    # Placeholders carry no fabricated page/caption.
    for table in result.detected_tables:
        assert table.page is None
        assert table.caption is None


def test_detected_tables_empty_when_count_zero():
    result = normalize_compile_result(
        _activity_result(content_stats={"table_count": 0}),
        document_id="doc-1",
    )
    assert result.detected_tables == ()


# ---- detected_content_types ----------------------------------------


def test_detected_content_types_combines_signals():
    result = normalize_compile_result(
        _activity_result(content_stats={
            "has_images": True, "has_tables": True,
            "image_count": 2, "table_count": 3,
            "text_block_count": 40, "total_text_chars": 8000,
        }),
        document_id="doc-1",
    )
    # Order matches `_build_extraction_evidence` so the FE can
    # reuse its existing label table.
    assert "text" in result.detected_content_types
    assert "images" in result.detected_content_types
    assert "tables" in result.detected_content_types


def test_detected_content_types_includes_scanned_pages_flag():
    result = normalize_compile_result(
        _activity_result(content_stats={"has_scanned_pages": True}),
        document_id="doc-1",
    )
    assert "scanned_pages" in result.detected_content_types


def test_detected_content_types_includes_equations():
    result = normalize_compile_result(
        _activity_result(content_stats={"equation_count": 2}),
        document_id="doc-1",
    )
    assert "equations" in result.detected_content_types


# ---- quality_signals -----------------------------------------------


def test_quality_signals_projects_every_score():
    result = normalize_compile_result(
        _activity_result(content_stats={
            "parse_quality_score": 0.85,
            "text_sufficiency_score": 0.9,
            "layout_complexity_score": 0.4,
            "empty_page_ratio": 0.05,
            "text_extractable_ratio": 1.0,
        }),
        document_id="doc-1",
    )
    assert result.quality_signals == CompileQualitySignals(
        parse_quality_score=0.85,
        text_sufficiency_score=0.9,
        layout_complexity_score=0.4,
        empty_page_ratio=0.05,
        text_extractable_ratio=1.0,
    )


def test_quality_signals_missing_become_none():
    result = normalize_compile_result(
        _activity_result(),
        document_id="doc-1",
    )
    sig = result.quality_signals
    assert sig.parse_quality_score is None
    assert sig.text_sufficiency_score is None
    assert sig.layout_complexity_score is None


# ---- retry_history --------------------------------------------------


def test_retry_history_projects_per_attempt_dicts():
    attempts = [
        {"attempt_number": 1, "mode": "standard", "parser": "raganything",
         "parse_method": "auto", "status": "succeeded",
         "chunks_count": 0, "extracted_text_chars": 0,
         "quality": "low", "retry_reason": "zero chunks; escalating to deep"},
        {"attempt_number": 2, "mode": "deep", "parser": "raganything",
         "parse_method": "auto", "status": "succeeded",
         "chunks_count": 5, "extracted_text_chars": 8000,
         "quality": "good", "retry_reason": None},
    ]
    result = normalize_compile_result(
        _activity_result(), document_id="doc-1", retry_attempts=attempts,
    )
    assert len(result.retry_history) == 2
    assert result.retry_history[0].mode == "standard"
    assert result.retry_history[0].quality == "low"
    assert result.retry_history[1].mode == "deep"
    assert result.retry_history[1].chunks_count == 5
    # `final_compile_mode` mirrors the last attempt's mode.
    assert result.final_compile_mode == "deep"


def test_retry_history_is_empty_when_no_attempts_provided():
    """Caller didn't track retries → empty history. Final compile
    mode falls back to `assessment_mode` in metrics when present."""
    result = normalize_compile_result(
        _activity_result(compile_metrics={"assessment_mode": "standard"}),
        document_id="doc-1",
    )
    assert result.retry_history == ()
    assert result.final_compile_mode == "standard"


def test_retry_history_tolerates_malformed_entries():
    """Workflow audit dicts can be incomplete (e.g. a crashed
    attempt that never finished recording). Drop non-dicts; coerce
    missing fields to defaults."""
    attempts = [
        {"attempt_number": 1, "status": "succeeded", "mode": "standard"},
        "not-a-dict",
        {"attempt_number": 2},  # mostly empty
    ]
    records = build_compile_attempt_records(attempts)
    assert len(records) == 2
    assert records[0].mode == "standard"
    assert records[1].attempt_number == 2
    assert records[1].status == "unknown"


# ---- raw_artifact_refs ----------------------------------------------


def test_raw_artifact_refs_preserve_vendor_ids():
    """Raw vendor output stays in the workspace — the normalized
    result references it by id only."""
    result = normalize_compile_result(
        _activity_result(
            artifact_ids=["a-1", "a-2", "a-3"],
            kinds=("chunk", "chunk", "parsed_content_manifest"),
        ),
        document_id="doc-1",
    )
    assert result.raw_artifact_refs == ("a-1", "a-2", "a-3")
    assert result.artifact_kinds == ("chunk", "chunk", "parsed_content_manifest")


# ---- warnings + errors ----------------------------------------------


def test_warnings_combine_plan_warnings_and_caller_extras():
    result = normalize_compile_result(
        _activity_result(compile_metrics={
            "plan_warnings": ["low-density page 3", "no language detected"],
        }),
        document_id="doc-1",
        extra_warnings=("retry escalation: standard → deep",),
    )
    assert "low-density page 3" in result.warnings
    assert "no language detected" in result.warnings
    assert "retry escalation: standard → deep" in result.warnings


def test_errors_surface_activity_error_message():
    result = normalize_compile_result(
        _activity_result(status="failed", error="vendor crashed"),
        document_id="doc-1",
    )
    assert result.status == "failed"
    assert "vendor crashed" in result.errors


def test_errors_empty_on_success():
    result = normalize_compile_result(
        _activity_result(),
        document_id="doc-1",
    )
    assert result.errors == ()


# ---- duration_ms ----------------------------------------------------


def test_duration_ms_carried_through_when_supplied():
    result = normalize_compile_result(
        _activity_result(),
        document_id="doc-1",
        duration_ms=42_500,
    )
    assert result.duration_ms == 42_500


# ---- round-trip -----------------------------------------------------


def test_to_payload_from_payload_round_trips():
    original = normalize_compile_result(
        _activity_result(
            artifact_ids=["a-1"],
            kinds=("chunk",),
            content_stats={
                "has_images": True, "image_count": 1, "page_count": 5,
                "images": [{"image_id": "img-1", "page": 1}],
                "table_count": 2,
                "parse_quality_score": 0.9,
            },
            compile_metrics={"chunks_count": 1, "extracted_text_chars": 100},
        ),
        document_id="doc-1",
        retry_attempts=[
            {"attempt_number": 1, "mode": "standard", "status": "succeeded",
             "chunks_count": 1, "extracted_text_chars": 100,
             "warnings": ["w1", "w2"]},
        ],
        final_quality_verdict="good",
        duration_ms=10_000,
    )
    restored = NormalizedCompileResult.from_payload(original.to_payload())
    assert restored == original


# ---- raw vendor output preserved + no expensive imports ------------


def test_normalizer_module_does_not_import_llm_or_vendor_clients():
    """Wave 4 is an adapter — no LLM / vendor coupling allowed in
    the normalizer. AST-check imports to catch regressions."""
    mod = sys.modules.get("j1.processing.compile_result")
    assert mod is not None
    tree = ast.parse(inspect.getsource(mod))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden_prefixes = (
        "j1.llm",
        "j1.providers.raganything",
        "j1.providers.mineru",
        "openai",
        "anthropic",
    )
    for name in imported:
        for prefix in forbidden_prefixes:
            assert not name.startswith(prefix), (
                f"compile_result.py unexpectedly imports {name!r}; "
                "the normalizer must stay an adapter over the "
                "activity result, not a vendor-coupled module."
            )


# ---- determinism ---------------------------------------------------


def test_normalizer_is_deterministic_for_identical_inputs():
    """Same activity result + same retry history → same normalized
    payload. Required for Temporal workflow replay."""
    attempts = [{"attempt_number": 1, "status": "succeeded", "mode": "standard"}]
    a = normalize_compile_result(
        _activity_result(compile_metrics={"chunks_count": 2}),
        document_id="doc-1",
        retry_attempts=attempts,
    )
    b = normalize_compile_result(
        _activity_result(compile_metrics={"chunks_count": 2}),
        document_id="doc-1",
        retry_attempts=attempts,
    )
    assert a.to_payload() == b.to_payload()
