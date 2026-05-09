"""PlanningResult schema + LLM-output validator tests.

Mirrors the spec's "Testing requirements" §4: invalid LLM output is
rejected, no raw content leaks past the validator, fail-open path
falls back to rule-based, the persisted artifact round-trips."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.processing.content_digest import build_content_digest
from j1.processing.document_understanding import (
    DocumentMetadata,
    assess_document_understanding,
)
from j1.processing.manifest import (
    ParsedContentItem,
    ParsedContentManifest,
    ParsedContentStats,
)
from j1.processing.planning_result import (
    PlanningResult,
    PlanningValidationError,
    assessment_to_planning_result,
    validate_planning_result_dict,
)
from j1.processing.planning_settings import PlanningSettings
from j1.processing.post_compile_assessment import build_post_compile_assessment
from j1.processing.post_compile_planning import build_planning_result


def _manifest(*, page_count: int = 8) -> ParsedContentManifest:
    items = [
        ParsedContentItem(
            item_id="t1", type="heading", page_idx=1,
            text_preview="System Requirement Specification for J1",
        ),
    ]
    return ParsedContentManifest(
        document_id="doc-1", document_hash="h", parser="raganything",
        parser_version="1", parse_method="auto", profile=None,
        stats=ParsedContentStats(
            text_blocks=80, tables=2, images=1, equations=0,
            total_items=83, page_count=page_count,
            parse_quality_score=0.9,
        ),
        items=items,
    )


def _basic_assessment():
    manifest = _manifest()
    md = DocumentMetadata(document_id="doc-1", filename="srs.pdf")
    u = assess_document_understanding(metadata=md, manifest=manifest)
    digest = build_content_digest(
        manifest=manifest, understanding=u,
        max_sample_blocks=20, max_preview_chars=300, max_early_pages=3,
    )
    return build_post_compile_assessment(
        understanding=u, manifest=manifest, profile=None, digest=digest,
    )


# ---- Schema round-trip -----------------------------------------------


def test_assessment_round_trips_through_planning_result():
    a = _basic_assessment()
    result = assessment_to_planning_result(
        run_id="r1", document_id="doc-1",
        created_at="2026-05-09T00:00:00Z",
        assessment=a,
    )
    payload = result.to_dict()
    rebuilt = PlanningResult.from_dict(payload)
    assert rebuilt.run_id == "r1"
    assert rebuilt.recommended_profile == a.recommended_profile
    assert rebuilt.document_understanding["document_type"] == "system_requirement_specification"
    assert "table_enrichment" in rebuilt.execution_plan.get("steps", {})


# ---- Validator -------------------------------------------------------


def _good_payload(page_count: int = 8) -> dict:
    return {
        "planning_version": "1.0",
        "recommended_profile": "balanced",
        "confidence": 0.8,
        "document_understanding": {"document_type": "report"},
        "decision_summary": {},
        "content_report": {},
        "quality_report": {"parse_confidence": "high"},
        "execution_plan": {
            "estimated_time": "low",
            "estimated_cost": "low",
            "steps": {
                "chunking": {
                    "enabled": True,
                    "strategy": "section_aware",
                    "reason": "clear headings",
                },
                "embedding": {
                    "enabled": True, "scope": "document",
                    "reason": "needed for retrieval",
                },
                "indexing": {
                    "enabled": True, "scope": "document",
                    "reason": "needed for retrieval",
                },
                "vision_enrichment": {
                    "enabled": False, "scope": "none",
                    "reason": "no images",
                    "pages": [],
                },
            },
        },
        "rule_based_comparison": {},
        "warnings": [],
        "next_actions": [],
    }


def test_validator_accepts_clean_payload():
    validate_planning_result_dict(_good_payload(), page_count=8)


def test_validator_rejects_invalid_profile():
    payload = _good_payload()
    payload["recommended_profile"] = "ultra_premium"
    with pytest.raises(PlanningValidationError, match="recommended_profile"):
        validate_planning_result_dict(payload, page_count=8)


def test_validator_rejects_invalid_document_type():
    payload = _good_payload()
    payload["document_understanding"]["document_type"] = "spaceship"
    with pytest.raises(PlanningValidationError, match="document_type"):
        validate_planning_result_dict(payload, page_count=8)


def test_validator_rejects_out_of_range_confidence():
    payload = _good_payload()
    payload["confidence"] = 1.5
    with pytest.raises(PlanningValidationError, match="confidence"):
        validate_planning_result_dict(payload, page_count=8)


def test_validator_rejects_step_without_reason():
    payload = _good_payload()
    payload["execution_plan"]["steps"]["vision_enrichment"]["reason"] = ""
    with pytest.raises(PlanningValidationError, match="reason"):
        validate_planning_result_dict(payload, page_count=8)


def test_validator_rejects_pages_outside_document():
    payload = _good_payload()
    payload["execution_plan"]["steps"]["vision_enrichment"]["enabled"] = True
    payload["execution_plan"]["steps"]["vision_enrichment"]["scope"] = "selected_pages"
    payload["execution_plan"]["steps"]["vision_enrichment"]["pages"] = [9, 10, 11]
    with pytest.raises(PlanningValidationError, match="page_count"):
        validate_planning_result_dict(payload, page_count=8)


def test_validator_blocks_full_document_content_leak():
    """Strings longer than the privacy cap fail validation — guards
    against the LLM echoing the entire document into a description
    field."""
    payload = _good_payload()
    payload["decision_summary"]["overall_assessment"] = "x" * 5000
    with pytest.raises(PlanningValidationError, match="raw document"):
        validate_planning_result_dict(payload, page_count=8)


# ---- LLM fail-open / fail-closed --------------------------------------


def test_llm_failure_falls_back_when_fail_open_true():
    """LLM raising → result.source becomes rule_based_fallback;
    workflow continues."""
    settings = PlanningSettings(
        llm_planning_enabled=True, fail_open=True,
    )

    def boom(_ctx):
        raise RuntimeError("provider down")

    result = build_planning_result(
        run_id="r1",
        document=DocumentMetadata(document_id="doc-1", filename="srs.pdf"),
        file_size_bytes=1024,
        profile=None,
        manifest=_manifest(),
        settings=settings,
        llm_planner=boom,
        now=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )
    assert result.source == "rule_based_fallback"
    assert any("LLM" in w or "fallback" in w.lower() for w in result.warnings)


def test_llm_failure_raises_when_fail_open_false():
    settings = PlanningSettings(
        llm_planning_enabled=True, fail_open=False,
    )

    def boom(_ctx):
        raise RuntimeError("provider down")

    with pytest.raises(PlanningValidationError):
        build_planning_result(
            run_id="r1",
            document=DocumentMetadata(document_id="doc-1", filename="srs.pdf"),
            file_size_bytes=1024,
            profile=None,
            manifest=_manifest(),
            settings=settings,
            llm_planner=boom,
        )


def test_llm_invalid_output_falls_back_when_fail_open():
    settings = PlanningSettings(
        llm_planning_enabled=True, fail_open=True,
    )

    def stub(_ctx):
        # Invalid: missing reason on the chunking entry.
        return {
            "planning_version": "1.0",
            "recommended_profile": "balanced",
            "confidence": 0.8,
            "document_understanding": {"document_type": "report"},
            "execution_plan": {
                "steps": {
                    "chunking": {
                        "enabled": True, "strategy": "section_aware",
                    },
                },
            },
        }

    result = build_planning_result(
        run_id="r1",
        document=DocumentMetadata(document_id="doc-1", filename="srs.pdf"),
        file_size_bytes=1024,
        profile=None,
        manifest=_manifest(),
        settings=settings,
        llm_planner=stub,
    )
    assert result.source == "rule_based_fallback"


def test_llm_accepted_output_marks_result_as_llm():
    """Valid LLM output replaces the rule-based plan and marks the
    source as `llm`. Comparison block shows what changed."""
    settings = PlanningSettings(
        llm_planning_enabled=True, fail_open=True,
    )
    payload = _good_payload(page_count=8)

    def stub(_ctx):
        return payload

    result = build_planning_result(
        run_id="r1",
        document=DocumentMetadata(document_id="doc-1", filename="srs.pdf"),
        file_size_bytes=1024,
        profile=None,
        manifest=_manifest(),
        settings=settings,
        llm_planner=stub,
    )
    assert result.source == "llm"
    assert result.recommended_profile == "balanced"
    # Rule-based result is preserved alongside.
    assert result.rule_based_assessment.get("recommended_profile")
    assert "accepted_rule_recommendations" in result.rule_based_comparison


# ---- Privacy: no raw content in result -------------------------------


def test_no_raw_document_content_in_result_payload():
    """The serialised PlanningResult must not contain any string
    longer than the privacy cap. Defence-in-depth on top of the
    validator."""
    a = _basic_assessment()
    result = assessment_to_planning_result(
        run_id="r1", document_id="doc-1",
        created_at="2026-05-09T00:00:00Z", assessment=a,
    )
    # Walking the dict: validator catches large strings.
    validate_planning_result_dict(result.to_dict(), page_count=8)
