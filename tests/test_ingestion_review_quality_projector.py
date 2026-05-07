"""Unit tests for QualityReportProjector — no service, no REST.

Exercises the projection rules directly:
  * Overall-confidence priority chain (explicit > modality mean >
    default_confidence > metadata.confidence > None).
  * Modality grouping with sample-count aggregation.
  * Low-confidence findings from both confidence + consistency
    sources, with traceability fields preserved.
  * Step-result splitting (skipped vs failed-optional).
  * `include_raw` toggle.
  * Resilience to missing files / bad JSON / wrong artifact kind.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.ingestion_review.dtos import WarningDTO
from j1.ingestion_review.projectors import QualityReportProjector
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def _record(
    artifact_id: str,
    location: str,
    *,
    kind: str,
    metadata: dict[str, Any] | None = None,
    source_artifact_ids: list[str] | None = None,
) -> ArtifactRecord:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ProjectContext(tenant_id="acme", project_id="alpha"),
        kind=kind,
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_artifact_ids=source_artifact_ids or [],
        metadata=metadata or {},
    )


def _resolver(mapping: dict[str, Path]):
    def _resolve(record: ArtifactRecord) -> Path:
        return mapping[record.artifact_id]
    return _resolve


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---- Empty / missing inputs ----------------------------------------


def test_project_returns_empty_report_for_empty_inputs():
    projector = QualityReportProjector(path_resolver=lambda _r: Path("/nope"))
    report = projector.project(
        artifacts=[], warnings=[], step_results=[],
    )
    assert report.overall_confidence is None
    assert report.modality_confidences == []
    assert report.warnings == []
    assert report.skipped_steps == []
    assert report.failed_optional_steps == []
    assert report.low_confidence_findings == []
    assert report.raw_debug is None


def test_project_skips_non_quality_artifacts(tmp_path):
    """Artifacts of other kinds must not feed the projector."""
    chunk = tmp_path / "ch.json"
    _write_json(chunk, {"chunk_id": "c", "body": "x"})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": chunk}))
    report = projector.project(
        artifacts=[_record("a1", "compiled/ch.json", kind="chunk")],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence is None
    assert report.modality_confidences == []


def test_project_skips_markdown_sibling(tmp_path):
    """Confidence-assessment .md siblings carry no signal — project
    only the .json."""
    md_path = tmp_path / "ca.md"
    md_path.write_text("# md only", encoding="utf-8")
    projector = QualityReportProjector(path_resolver=_resolver({"a1": md_path}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.md", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence is None


def test_project_resilient_to_missing_file(tmp_path):
    projector = QualityReportProjector(
        path_resolver=_resolver({"a1": tmp_path / "ghost.json"}),
    )
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ghost.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence is None


def test_project_resilient_to_bad_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    projector = QualityReportProjector(path_resolver=_resolver({"a1": bad}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/bad.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence is None


# ---- Overall confidence priority chain -----------------------------


def test_overall_confidence_uses_explicit_field_when_present(tmp_path):
    p = tmp_path / "ca.json"
    _write_json(p, {"overall_confidence": 0.91, "default_confidence": 0.5})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence == 0.91


def test_overall_confidence_falls_back_to_modality_mean(tmp_path):
    p = tmp_path / "ca.json"
    _write_json(p, {"assessments": [
        {"modality": "tables", "confidence": 0.8},
        {"modality": "images", "confidence": 0.6},
    ]})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence == 0.7


def test_overall_confidence_falls_back_to_default_field(tmp_path):
    p = tmp_path / "ca.json"
    _write_json(p, {"default_confidence": 0.42})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence == 0.42


def test_overall_confidence_falls_back_to_metadata(tmp_path):
    """The base enricher writes `metadata["confidence"]` as a string;
    the projector must coerce and surface it as a last resort."""
    p = tmp_path / "ca.json"
    _write_json(p, {})  # no payload-level confidence
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
            metadata={"confidence": "0.55"},
        )],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence == 0.55


def test_overall_confidence_none_when_no_data(tmp_path):
    p = tmp_path / "ca.json"
    _write_json(p, {"assessments": []})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert report.overall_confidence is None


# ---- Modality breakdown --------------------------------------------


def test_modality_confidences_group_and_average(tmp_path):
    p = tmp_path / "ca.json"
    _write_json(p, {"assessments": [
        {"modality": "tables", "confidence": 0.8, "sample_count": 3},
        {"modality": "tables", "confidence": 0.6, "sample_count": 2},
        {"modality": "images", "confidence": 0.4},
    ]})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    by_modality = {m.modality: m for m in report.modality_confidences}
    assert by_modality["tables"].confidence == 0.7  # mean of 0.8 + 0.6
    assert by_modality["tables"].sample_count == 5  # 3 + 2
    assert by_modality["images"].confidence == 0.4
    assert by_modality["images"].sample_count == 1  # default when missing


def test_modality_confidences_accept_camelcase(tmp_path):
    p = tmp_path / "ca.json"
    _write_json(p, {"assessments": [
        {"modality": "ocr", "confidence": 0.9, "sampleCount": 5},
    ]})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert report.modality_confidences[0].sample_count == 5


# ---- Low-confidence findings ---------------------------------------


def test_low_confidence_findings_from_assessments(tmp_path):
    """Confidence-assessment entries with score < 0.7 surface as
    findings; >= 0.7 are excluded as noise."""
    p = tmp_path / "ca.json"
    _write_json(p, {"assessments": [
        {"modality": "tables", "confidence": 0.85},  # excluded
        {"modality": "tables", "confidence": 0.45,
         "page": 7, "chunk_id": "ch-3", "category": "low_confidence"},
        {"modality": "ocr", "confidence": 0.3,
         "message": "page 12 OCR uncertain"},
    ]})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
        )],
        warnings=[], step_results=[],
    )
    assert len(report.low_confidence_findings) == 2
    first = report.low_confidence_findings[0]
    assert first.page == 7
    assert first.chunk_id == "ch-3"
    assert first.category == "low_confidence"
    assert first.score == 0.45
    second = report.low_confidence_findings[1]
    assert "OCR uncertain" in second.message


def test_low_confidence_findings_from_consistency(tmp_path):
    p = tmp_path / "consistency.json"
    _write_json(p, {"findings": [
        {"page": 3, "score": 0.2, "category": "duplicate",
         "message": "duplicate definition", "artifact_id": "x"},
        {"page": 4, "category": "missing_section",
         "message": "section header missing"},
    ]})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/consistency.json",
            kind="enriched.consistency_findings",
        )],
        warnings=[], step_results=[],
    )
    assert len(report.low_confidence_findings) == 2
    # The second entry had no score — projector defaults to 0.0.
    no_score = report.low_confidence_findings[1]
    assert no_score.score == 0.0
    assert no_score.category == "missing_section"


def test_finding_artifact_id_falls_back_to_producing_artifact(tmp_path):
    """Producer didn't pin `artifact_id` on the finding — the
    artifact that produced the finding becomes the lineage anchor."""
    p = tmp_path / "consistency.json"
    _write_json(p, {"findings": [
        {"score": 0.1, "category": "x"},
    ]})
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))
    report = projector.project(
        artifacts=[_record(
            "a1", "enriched/consistency.json",
            kind="enriched.consistency_findings",
        )],
        warnings=[], step_results=[],
    )
    assert report.low_confidence_findings[0].artifact_id == "a1"


# ---- Step outcomes -------------------------------------------------


def test_step_outcomes_split_skipped_vs_failed_optional():
    projector = QualityReportProjector(path_resolver=lambda _r: Path("/nope"))
    report = projector.project(
        artifacts=[], warnings=[],
        step_results=[
            {"step": "compile", "status": "completed", "required": True,
             "source": "caller"},
            {"step": "graph", "status": "skipped", "required": False,
             "source": "policy", "reason": "text-only mode"},
            {"step": "enrich", "status": "failed", "required": False,
             "source": "planner", "reason": "vision LLM unavailable",
             "error": {"type": "VisionUnavailableError"}},
            {"step": "indexer", "status": "failed", "required": True,
             "source": "default"},  # required → not in failed_optional
        ],
    )
    assert [s.step for s in report.skipped_steps] == ["graph"]
    assert report.skipped_steps[0].policy == "policy"
    assert report.skipped_steps[0].reason == "text-only mode"
    assert [f.step for f in report.failed_optional_steps] == ["enrich"]
    assert report.failed_optional_steps[0].error_type == "VisionUnavailableError"


def test_step_outcomes_drops_malformed_entries():
    projector = QualityReportProjector(path_resolver=lambda _r: Path("/nope"))
    report = projector.project(
        artifacts=[], warnings=[],
        step_results=[
            "not a dict",
            {"status": "skipped"},  # missing step
            {"step": "graph", "status": "skipped", "source": "planner"},
        ],
    )
    assert [s.step for s in report.skipped_steps] == ["graph"]


# ---- Warnings pass-through -----------------------------------------


def test_warnings_pass_through_with_traceability_preserved():
    warnings = [
        WarningDTO(
            code="step.warning", message="page 5 had degraded confidence",
            severity="warning", step="EXTRACT_TABLES", page=5,
        )
    ]
    projector = QualityReportProjector(path_resolver=lambda _r: Path("/nope"))
    report = projector.project(
        artifacts=[], warnings=warnings, step_results=[],
    )
    assert len(report.warnings) == 1
    assert report.warnings[0].page == 5
    assert report.warnings[0].step == "EXTRACT_TABLES"


# ---- raw_debug toggle ----------------------------------------------


def test_raw_debug_only_populated_when_include_raw(tmp_path):
    p = tmp_path / "ca.json"
    payload = {"assessments": [], "default_confidence": 0.8}
    _write_json(p, payload)
    artifact = _record(
        "a1", "enriched/ca.json", kind="enriched.confidence_assessment",
    )
    projector = QualityReportProjector(path_resolver=_resolver({"a1": p}))

    no_raw = projector.project(
        artifacts=[artifact], warnings=[], step_results=[],
    )
    assert no_raw.raw_debug is None

    with_raw = projector.project(
        artifacts=[artifact], warnings=[], step_results=[],
        include_raw=True,
    )
    assert with_raw.raw_debug is not None
    assert with_raw.raw_debug["confidence_assessment"][0] == payload
    assert with_raw.raw_debug["consistency_findings"] == []
