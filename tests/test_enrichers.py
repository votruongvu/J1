import json
from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.enrichers import (
    ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT,
    ARTIFACT_TYPE_CONSISTENCY_FINDINGS,
    ARTIFACT_TYPE_DOCUMENT_MAP,
    ARTIFACT_TYPE_FORMULAS,
    ARTIFACT_TYPE_REQUIREMENTS,
    ARTIFACT_TYPE_RISKS,
    ARTIFACT_TYPE_SOURCE_MAP,
    ARTIFACT_TYPE_TABLES,
    ARTIFACT_TYPE_VISUALS,
    GENERIC_ENRICHERS,
    ConfidenceAssessor,
    ConsistencyChecker,
    DocumentClassifier,
    FormulaExtractor,
    RequirementExtractor,
    RiskExtractor,
    SourceMapper,
    TableExtractor,
    VisualContentDescriber,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.results import ArtifactDraft
from j1.processing.status import ResultStatus
from j1.profiles import DEFAULT_PROFILE_ID, Profile, ProfileLoader


# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
def default_profile() -> Profile:
    return ProfileLoader().load(DEFAULT_PROFILE_ID)


def _artifact_record(ctx, *, artifact_id="art-1") -> ArtifactRecord:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="compiled.text",
        location=f"compiled/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
    )


# ---- Framework changes -------------------------------------------------


def test_artifact_draft_review_required_defaults_false():
    draft = ArtifactDraft(kind="x", content=b"y")
    assert draft.review_required is False


def test_artifact_draft_review_required_can_be_set():
    draft = ArtifactDraft(kind="x", content=b"y", review_required=True)
    assert draft.review_required is True


# ---- Per-processor coverage --------------------------------------------


_ALL_PROCESSORS = [
    (DocumentClassifier, ARTIFACT_TYPE_DOCUMENT_MAP, False, ("json", "md")),
    (RequirementExtractor, ARTIFACT_TYPE_REQUIREMENTS, False, ("json", "md")),
    (TableExtractor, ARTIFACT_TYPE_TABLES, False, ("json", "md")),
    (VisualContentDescriber, ARTIFACT_TYPE_VISUALS, True, ("json", "md")),
    (FormulaExtractor, ARTIFACT_TYPE_FORMULAS, True, ("json", "md")),
    (RiskExtractor, ARTIFACT_TYPE_RISKS, False, ("json", "md")),
    (ConsistencyChecker, ARTIFACT_TYPE_CONSISTENCY_FINDINGS, True, ("json", "md")),
    (SourceMapper, ARTIFACT_TYPE_SOURCE_MAP, False, ("json",)),
    (ConfidenceAssessor, ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT, False, ("json", "md")),
]


@pytest.mark.parametrize(
    "cls,expected_type,expected_review,expected_formats",
    _ALL_PROCESSORS,
    ids=[c.__name__ for c, *_ in _ALL_PROCESSORS],
)
def test_processor_produces_expected_artifact_type_and_formats(
    cls, expected_type, expected_review, expected_formats, default_profile, ctx
):
    proc = cls(default_profile)
    result = proc.enrich(ctx, "art-1")
    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.drafts) == len(expected_formats)
    assert all(d.kind == expected_type for d in result.drafts)
    assert all(d.review_required is expected_review for d in result.drafts)
    formats_seen = {d.metadata["format"] for d in result.drafts}
    expected_format_names = {
        "json" if f == "json" else "markdown" for f in expected_formats
    }
    assert formats_seen == expected_format_names


@pytest.mark.parametrize(
    "cls", [c for c, *_ in _ALL_PROCESSORS], ids=[c.__name__ for c, *_ in _ALL_PROCESSORS]
)
def test_processor_metadata_includes_required_fields(cls, default_profile, ctx):
    proc = cls(default_profile)
    result = proc.enrich(ctx, "art-1")
    for draft in result.drafts:
        meta = draft.metadata
        assert meta["processor_name"] == proc.kind
        assert meta["processor_version"] == proc.version
        assert meta["artifact_type"] == proc.artifact_type
        assert meta["source_artifact_id"] == "art-1"
        assert "confidence" in meta
        assert meta["review_required"] in ("true", "false")
        assert "prompt_name" in meta


@pytest.mark.parametrize(
    "cls", [c for c, *_ in _ALL_PROCESSORS], ids=[c.__name__ for c, *_ in _ALL_PROCESSORS]
)
def test_processor_includes_source_traceability(cls, default_profile, ctx):
    proc = cls(default_profile)
    result = proc.enrich(ctx, "art-1")
    for draft in result.drafts:
        assert draft.source_artifact_ids == ["art-1"]


@pytest.mark.parametrize(
    "cls", [c for c, *_ in _ALL_PROCESSORS], ids=[c.__name__ for c, *_ in _ALL_PROCESSORS]
)
def test_processor_disabled_returns_skipped(cls, default_profile, ctx):
    proc = cls(default_profile, enabled=False)
    result = proc.enrich(ctx, "art-1")
    assert result.status is ResultStatus.SKIPPED
    assert result.drafts == []


def test_review_required_processors_are_visual_formula_consistency():
    """Sanity: only the three processors specified in the spec default to review_required."""
    review_required = {
        cls.__name__
        for cls, _t, review, _f in _ALL_PROCESSORS
        if review
    }
    assert review_required == {
        "VisualContentDescriber",
        "FormulaExtractor",
        "ConsistencyChecker",
    }


def test_source_mapper_only_emits_json(default_profile, ctx):
    proc = SourceMapper(default_profile)
    result = proc.enrich(ctx, "art-1")
    assert len(result.drafts) == 1
    assert result.drafts[0].suggested_extension == ".json"


# ---- JSON content sanity ----------------------------------------------


def test_json_content_is_valid_and_carries_source_artifact_id(default_profile, ctx):
    proc = DocumentClassifier(default_profile)
    result = proc.enrich(ctx, "art-1")
    json_draft = next(d for d in result.drafts if d.suggested_extension == ".json")
    parsed = json.loads(json_draft.content.decode("utf-8"))
    assert parsed["source_artifact_id"] == "art-1"


# ---- Profile prompt usage ---------------------------------------------


def test_processor_records_prompt_was_used_when_profile_supplies_it(ctx):
    profile = Profile(
        profile_id="custom",
        metadata={},
        prompts={"classify_document": "Classify this document carefully."},
    )
    proc = DocumentClassifier(profile)
    result = proc.enrich(ctx, "art-1")
    json_draft = next(d for d in result.drafts if d.suggested_extension == ".json")
    parsed = json.loads(json_draft.content.decode("utf-8"))
    assert parsed["prompt_used"] is True


def test_processor_records_prompt_not_used_when_profile_lacks_it(ctx):
    profile = Profile(profile_id="empty", metadata={})
    proc = DocumentClassifier(profile)
    result = proc.enrich(ctx, "art-1")
    json_draft = next(d for d in result.drafts if d.suggested_extension == ".json")
    parsed = json.loads(json_draft.content.decode("utf-8"))
    assert parsed["prompt_used"] is False


# ---- Failure handling --------------------------------------------------


def test_processor_returns_failed_when_produce_raises(default_profile, ctx):
    class _BoomEnricher(DocumentClassifier):
        def _produce(self, ctx, artifact_id):
            raise RuntimeError("kaboom")

    proc = _BoomEnricher(default_profile)
    result = proc.enrich(ctx, "art-1")
    assert result.status is ResultStatus.FAILED
    assert result.error == "kaboom"
    assert result.message == "RuntimeError"


def test_processor_passes_content_through_source(default_profile, ctx):
    captured = {}

    def reader(ctx_in, artifact_id):
        captured["ctx"] = ctx_in
        captured["id"] = artifact_id
        return b"hello world"

    proc = SourceMapper(default_profile, content_source=reader)
    result = proc.enrich(ctx, "art-1")
    parsed = json.loads(result.drafts[0].content.decode("utf-8"))
    assert parsed["sources"][0]["byte_size"] == len(b"hello world")
    assert captured["id"] == "art-1"


# ---- ProcessingService integration ------------------------------------


def test_review_required_propagates_to_artifact_record(
    processing_service, artifact_registry, default_profile, ctx
):
    """End-to-end: visual enricher → ProcessingService.enrich → ArtifactRecord
    has review_status=PENDING (because VisualContentDescriber defaults review_required=True)."""
    artifact_registry.add(_artifact_record(ctx))
    proc = VisualContentDescriber(default_profile)
    result = processing_service.enrich(ctx, proc, _artifact_record(ctx))

    assert result.status is ResultStatus.SUCCEEDED
    assert all(
        r.review_status is ReviewStatus.PENDING for r in result.artifacts
    )
    listed = artifact_registry.list_artifacts(ctx, kind=ARTIFACT_TYPE_VISUALS)
    assert listed
    assert all(r.review_status is ReviewStatus.PENDING for r in listed)


def test_non_review_processor_yields_not_required_review_status(
    processing_service, artifact_registry, default_profile, ctx
):
    artifact_registry.add(_artifact_record(ctx))
    proc = TableExtractor(default_profile)
    result = processing_service.enrich(ctx, proc, _artifact_record(ctx))
    assert result.status is ResultStatus.SUCCEEDED
    assert all(
        r.review_status is ReviewStatus.NOT_REQUIRED for r in result.artifacts
    )


def test_outputs_are_stored_under_enriched_area(
    processing_service, workspace, artifact_registry, default_profile, ctx
):
    artifact_registry.add(_artifact_record(ctx))
    proc = RequirementExtractor(default_profile)
    result = processing_service.enrich(ctx, proc, _artifact_record(ctx))
    for record in result.artifacts:
        assert record.location.startswith("enriched/")
        path = workspace.enriched(ctx) / record.location.split("/", 1)[1]
        assert path.is_file()


def test_audit_event_recorded_for_enrichment(
    processing_service, workspace, artifact_registry, default_profile, ctx
):
    from j1.audit.sink import AUDIT_LOG_FILENAME

    artifact_registry.add(_artifact_record(ctx))
    proc = RequirementExtractor(default_profile)
    processing_service.enrich(ctx, proc, _artifact_record(ctx))

    events = [
        json.loads(line)
        for line in (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()
        if line.strip()
    ]
    enrich_events = [e for e in events if "enrich" in e["action"]]
    assert enrich_events


# ---- Catalog -----------------------------------------------------------


def test_generic_enrichers_catalog_lists_nine_processors():
    assert len(GENERIC_ENRICHERS) == 9
    names = {cls.__name__ for cls in GENERIC_ENRICHERS}
    assert names == {
        "DocumentClassifier",
        "RequirementExtractor",
        "TableExtractor",
        "VisualContentDescriber",
        "FormulaExtractor",
        "RiskExtractor",
        "ConsistencyChecker",
        "SourceMapper",
        "ConfidenceAssessor",
    }
