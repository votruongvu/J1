"""Tests for the LLM-backed implementations of the generic enrichers.

Until the previous turn, every non-VCD enricher was a stub: empty
arrays + "Total: 0" markdown. The Option-3 work upgrades them to
real `text_client.extract(...)` calls with structured-output
schemas. These tests pin:

  * Stub fallback when no `text_client` is wired (legacy contract).
  * Real extraction when a client IS wired.
  * Skip path when the artifact kind isn't text-shaped.
  * LLM error handling (soft skip + error field).
  * Per-enricher schema + markdown rendering.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from j1.enrichers import (
    ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT,
    ARTIFACT_TYPE_CONSISTENCY_FINDINGS,
    ARTIFACT_TYPE_FORMULAS,
    ARTIFACT_TYPE_REQUIREMENTS,
    ARTIFACT_TYPE_RISKS,
    ARTIFACT_TYPE_TABLES,
    ConfidenceAssessor,
    ConsistencyChecker,
    DocumentClassifier,
    FormulaExtractor,
    RequirementExtractor,
    RiskExtractor,
    TableExtractor,
    _is_text_kind,
)
from j1.processing.results import ArtifactDraft
from j1.processing.status import ResultStatus
from j1.profiles.model import Profile
from j1.projects.context import ProjectContext


# ---- Stub text client ---------------------------------------------


class _StubTextClient:
    """Returns a canned JSON response from `extract` so tests can
    drive each enricher end-to-end without a real LLM."""

    provider = "stub"
    model = "stub-text"

    def __init__(
        self,
        *,
        extract_response: dict | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._response = extract_response or {}
        self._raises = raises
        self.calls: list[dict] = []

    def extract(
        self,
        prompt: str,
        schema: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[dict, Any]:
        self.calls.append(
            {"prompt": prompt, "schema": schema, "metadata": dict(metadata or {})}
        )
        if self._raises is not None:
            raise self._raises
        return self._response, None


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


@pytest.fixture
def empty_profile() -> Profile:
    return Profile(profile_id="default", metadata={})


def _content_source(_text: bytes):
    """Build a minimal `content_source` returning the given bytes
    for any artifact_id. The enrichers don't care which id; the
    text content drives extraction."""
    def _src(_ctx, _artifact_id: str) -> bytes:
        return _text
    return _src


# ---- Stub fallback (no text_client wired) -------------------------


@pytest.mark.parametrize("cls,output_key,artifact_type", [
    (RequirementExtractor, "requirements", ARTIFACT_TYPE_REQUIREMENTS),
    (TableExtractor, "tables", ARTIFACT_TYPE_TABLES),
    (FormulaExtractor, "formulas", ARTIFACT_TYPE_FORMULAS),
    (RiskExtractor, "risks", ARTIFACT_TYPE_RISKS),
    (ConsistencyChecker, "findings", ARTIFACT_TYPE_CONSISTENCY_FINDINGS),
    (ConfidenceAssessor, "assessments", ARTIFACT_TYPE_CONFIDENCE_ASSESSMENT),
])
def test_returns_empty_stub_when_no_text_client(
    cls, output_key, artifact_type, empty_profile, ctx,
):
    """Without a text_client, every LLM-backed enricher must
    degrade to the legacy empty-output contract — same shape the
    stubs used to produce. This is what makes adopting the new
    base class safe for deployments that haven't wired LLMs."""
    enricher = cls(empty_profile)
    result = enricher.enrich(ctx, "art-1")
    assert result.status is ResultStatus.SUCCEEDED
    json_draft = next(d for d in result.drafts if d.suggested_extension == ".json")
    parsed = json.loads(json_draft.content.decode("utf-8"))
    assert parsed[output_key] == []


# ---- Real extraction (text_client wired) -------------------------


def test_table_extractor_returns_real_tables_from_llm(empty_profile, ctx):
    client = _StubTextClient(extract_response={
        "tables": [
            {
                "title": "Revenue by segment",
                "columns": ["segment", "q3", "q4"],
                "rows": [["Cloud", 412, 478], ["Hardware", 318, 296]],
                "page": 5,
            },
        ],
    })
    extractor = TableExtractor(
        empty_profile,
        text_client=client,
        content_source=_content_source(b"some markdown text\n\n| a | b |"),
    )
    result = extractor.enrich(ctx, "art-1")

    assert result.status is ResultStatus.SUCCEEDED
    json_draft = next(d for d in result.drafts if d.suggested_extension == ".json")
    parsed = json.loads(json_draft.content.decode("utf-8"))
    assert len(parsed["tables"]) == 1
    assert parsed["tables"][0]["columns"] == ["segment", "q3", "q4"]
    assert parsed["tables"][0]["rows"][0] == ["Cloud", 412, 478]
    # The markdown sibling renders the table.
    md = next(d for d in result.drafts if d.suggested_extension == ".md").content.decode("utf-8")
    assert "Revenue by segment" in md
    assert "| segment | q3 | q4 |" in md
    assert "| Cloud | 412 | 478 |" in md
    # The LLM was called with our schema.
    assert client.calls
    assert "tables" in client.calls[0]["schema"]["properties"]


def test_requirement_extractor_returns_real_requirements(empty_profile, ctx):
    client = _StubTextClient(extract_response={
        "requirements": [
            {"id": "REQ-001", "text": "The system MUST authenticate users.",
             "priority": "MUST"},
            {"text": "The UI SHOULD support dark mode.", "priority": "SHOULD"},
        ],
    })
    extractor = RequirementExtractor(
        empty_profile,
        text_client=client,
        content_source=_content_source(b"REQ-001: The system must..."),
    )
    result = extractor.enrich(ctx, "art-1")

    parsed = json.loads(
        next(d for d in result.drafts if d.suggested_extension == ".json").content,
    )
    assert len(parsed["requirements"]) == 2
    assert parsed["requirements"][0]["priority"] == "MUST"
    md = next(d for d in result.drafts if d.suggested_extension == ".md").content.decode("utf-8")
    assert "[MUST]" in md and "REQ-001" in md


def test_risk_extractor_renders_severity_and_category(empty_profile, ctx):
    client = _StubTextClient(extract_response={
        "risks": [
            {"title": "Currency volatility", "severity": "high",
             "category": "financial"},
            {"title": "Vendor dependency", "severity": "medium",
             "category": "operational"},
        ],
    })
    extractor = RiskExtractor(
        empty_profile,
        text_client=client,
        content_source=_content_source(b"forward-looking statements"),
    )
    result = extractor.enrich(ctx, "art-1")
    md = next(d for d in result.drafts if d.suggested_extension == ".md").content.decode("utf-8")
    assert "(high)" in md and "[financial]" in md
    assert "(medium)" in md and "[operational]" in md


def test_formula_extractor_renders_tex(empty_profile, ctx):
    client = _StubTextClient(extract_response={
        "formulas": [
            {"tex": "E = mc^2", "description": "Mass-energy equivalence"},
        ],
    })
    extractor = FormulaExtractor(
        empty_profile,
        text_client=client,
        content_source=_content_source(b"Einstein's equation says..."),
    )
    result = extractor.enrich(ctx, "art-1")
    md = next(d for d in result.drafts if d.suggested_extension == ".md").content.decode("utf-8")
    assert "`E = mc^2`" in md
    assert "Mass-energy equivalence" in md


def test_consistency_checker_renders_findings(empty_profile, ctx):
    client = _StubTextClient(extract_response={
        "findings": [
            {"category": "duplicate", "message": "Section 3.2 repeats 2.4",
             "page": 12, "score": 0.85},
            {"category": "contradiction", "message": "Says 5% AND 8%",
             "score": 0.7},
        ],
    })
    checker = ConsistencyChecker(
        empty_profile,
        text_client=client,
        content_source=_content_source(b"long document"),
    )
    result = checker.enrich(ctx, "art-1")
    md = next(d for d in result.drafts if d.suggested_extension == ".md").content.decode("utf-8")
    assert "[duplicate]" in md
    assert "(p. 12)" in md
    assert "[contradiction]" in md


def test_confidence_assessor_renders_overall_and_modalities(empty_profile, ctx):
    client = _StubTextClient(extract_response={
        "overall_confidence": 0.78,
        "assessments": [
            {"modality": "tables", "confidence": 0.86},
            {"modality": "ocr", "confidence": 0.55,
             "page": 7, "message": "OCR uncertain on page 7"},
        ],
    })
    assessor = ConfidenceAssessor(
        empty_profile,
        text_client=client,
        content_source=_content_source(b"some content"),
    )
    result = assessor.enrich(ctx, "art-1")
    parsed = json.loads(
        next(d for d in result.drafts if d.suggested_extension == ".json").content,
    )
    assert parsed["overall_confidence"] == 0.78
    assert len(parsed["assessments"]) == 2
    md = next(d for d in result.drafts if d.suggested_extension == ".md").content.decode("utf-8")
    assert "Overall confidence: 0.78" in md
    assert "tables: 0.86" in md
    assert "OCR uncertain on page 7" in md


def test_document_classifier_returns_real_sections_and_classification(empty_profile, ctx):
    client = _StubTextClient(extract_response={
        "classification": [{"label": "report", "confidence": 0.9}],
        "sections": [
            {"title": "Executive summary", "page_start": 1},
            {"title": "Findings", "page_start": 5},
        ],
    })
    classifier = DocumentClassifier(
        empty_profile,
        text_client=client,
        content_source=_content_source(b"# Executive summary\nblah blah"),
    )
    result = classifier.enrich(ctx, "art-1")
    parsed = json.loads(
        next(d for d in result.drafts if d.suggested_extension == ".json").content,
    )
    # Both classification + sections survive in the JSON output.
    assert parsed["classification"][0]["label"] == "report"
    assert len(parsed["sections"]) == 2
    # Legacy `prompt_used` field still present for backward compat.
    assert "prompt_used" in parsed
    # byte_size still emitted.
    assert parsed["byte_size"] > 0


# ---- Kind gate (text_kind predicate) ------------------------------


@pytest.mark.parametrize("cls", [
    DocumentClassifier, RequirementExtractor, TableExtractor,
    FormulaExtractor, RiskExtractor, ConsistencyChecker, ConfidenceAssessor,
])
def test_skips_non_text_artifact_when_lookup_provided(cls, empty_profile, ctx):
    """All LLM-backed text enrichers must skip image/graph artifacts
    when an `artifact_lookup` is wired. Without this, the workflow
    would feed image bytes to a text LLM and pollute the Assets
    cards with garbage extractions."""
    def _lookup(_ctx, _artifact_id: str) -> str:
        return "compile.image"

    client = _StubTextClient(extract_response={"items": []})
    extractor = cls(
        empty_profile,
        text_client=client,
        artifact_lookup=_lookup,
        content_source=_content_source(b"text"),
    )
    result = extractor.enrich(ctx, "art-1")
    assert result.status is ResultStatus.SKIPPED
    assert result.metadata["skip_reason"] == "non_text_artifact"
    assert result.metadata["artifact_kind"] == "compile.image"
    # LLM was NEVER called for skipped artifacts.
    assert not client.calls


def test_runs_for_text_kinds(empty_profile, ctx):
    """Text-shaped kinds (chunk, compile, compile.metadata) must
    NOT skip — they're the inputs the enrichers exist for."""
    for kind in ("chunk", "compile", "compile.metadata", "compile.text"):
        def _lookup(_ctx, _artifact_id: str, _k=kind) -> str:
            return _k
        client = _StubTextClient(extract_response={"requirements": []})
        extractor = RequirementExtractor(
            empty_profile,
            text_client=client,
            artifact_lookup=_lookup,
            content_source=_content_source(b"some text"),
        )
        result = extractor.enrich(ctx, "art-1")
        assert result.status is ResultStatus.SUCCEEDED, (
            f"unexpectedly skipped for kind={kind!r}"
        )


# ---- LLM error handling -------------------------------------------


def test_llm_error_yields_error_field_not_crash(empty_profile, ctx):
    """A flaky LLM call must NOT bubble up — it surfaces as an
    `error` field on the JSON draft so the operator sees what
    happened without the workflow failing."""
    client = _StubTextClient(raises=RuntimeError("rate limited"))
    extractor = TableExtractor(
        empty_profile,
        text_client=client,
        content_source=_content_source(b"text"),
    )
    result = extractor.enrich(ctx, "art-1")
    assert result.status is ResultStatus.SUCCEEDED
    parsed = json.loads(
        next(d for d in result.drafts if d.suggested_extension == ".json").content,
    )
    assert parsed["tables"] == []
    assert "RuntimeError" in parsed["error"]
    assert "rate limited" in parsed["error"]


# ---- The shared text-kind helper ---------------------------------


def test_is_text_kind_recognises_text_shapes():
    assert _is_text_kind("chunk") is True
    assert _is_text_kind("compile") is True
    assert _is_text_kind("compile.metadata") is True
    assert _is_text_kind("enriched.tables") is True  # text-shaped output
    # Non-text → False
    assert _is_text_kind("compile.image") is False
    assert _is_text_kind("graph_json") is False
    assert _is_text_kind("enriched.visuals") is False
    # Empty → True (legacy fallback when no lookup wired)
    assert _is_text_kind("") is True
    assert _is_text_kind(None) is True


# ---- Empty content guard ------------------------------------------


def test_empty_content_falls_back_to_stub(empty_profile, ctx):
    """When `content_source` returns b"" (artifact missing on disk
    / not yet written), the enricher must NOT hit the LLM with an
    empty body. Falls through to the legacy empty-output stub."""
    client = _StubTextClient(extract_response={"tables": [{"x": 1}]})
    extractor = TableExtractor(
        empty_profile,
        text_client=client,
        content_source=_content_source(b""),  # empty
    )
    result = extractor.enrich(ctx, "art-1")
    assert result.status is ResultStatus.SUCCEEDED
    parsed = json.loads(
        next(d for d in result.drafts if d.suggested_extension == ".json").content,
    )
    assert parsed["tables"] == []
    # LLM was NOT called (saves us a wasted token call).
    assert not client.calls
