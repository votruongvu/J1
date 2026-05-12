"""Regression tests for citation-preview enrichment in the
validation runner.

Bug fixed:
   The groundedness judge LLM receives citations as a list of
   ``[N] artifact_id @ location`` lineage lines unless the
   citation carries a non-empty ``preview``. Earlier the runner
   built citations without preview, so the judge had nothing to
   verify claims against — it correctly inferred "no evidence
   shown, all claims unsupported" and flagged everything as
   moderate-severity. Operators saw 2-3 unsupported-claim
   warnings on otherwise-grounded answers in every retrieval /
   smoke case.

   The runner now enriches ``ValidationCitationDTO.preview`` with
   the SAME body text the synthesizer sees (via the shared
   ``build_evidence_blocks`` helper), so the judge can ground.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.validation.dtos import (
    RetrievedChunkRefDTO,
    ValidationCitationDTO,
)


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


class _StubRegistry:
    def __init__(self, records):
        self._by_id = {r.artifact_id: r for r in records}

    def get(self, ctx, artifact_id):  # noqa: ARG002
        from j1.artifacts.registry import ArtifactNotFoundError
        if artifact_id not in self._by_id:
            raise ArtifactNotFoundError(artifact_id)
        return self._by_id[artifact_id]

    def list_artifacts(self, ctx, *, kind=None):  # noqa: ARG002
        return list(self._by_id.values())


# ---- DTO surface --------------------------------------------------


def test_validation_citation_dto_has_preview_field():
    """Schema regression: the DTO carries ``preview`` (defaults to
    None). Pinned so a future refactor that drops it shows up
    immediately."""
    citation = ValidationCitationDTO(
        artifact_id="art-1",
        artifact_type="chunk",
    )
    assert hasattr(citation, "preview")
    assert citation.preview is None


def test_validation_citation_dto_preview_is_first_class_field():
    """Constructor accepts ``preview`` — it's not a metadata key
    or computed property."""
    citation = ValidationCitationDTO(
        artifact_id="art-1",
        artifact_type="chunk",
        preview="some real body text",
    )
    assert citation.preview == "some real body text"


# ---- Runner enrichment behaviour ---------------------------------


def test_runner_enriches_citation_preview_with_chunk_body(
    tmp_path: Path, ctx,
):
    """Headline regression: a chunk citation comes in WITHOUT a
    preview; the runner enriches it with real body text loaded
    via the shared evidence builder."""
    from j1.validation.runner import DefaultValidationRunner

    # Stage a chunk NDJSON file containing real body text.
    chunk_path = tmp_path / "compiled" / "art-chunk.ndjson"
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_path.write_text(
        json.dumps({
            "chunk_id": "ch-a",
            "body": "The proposal due date is 20 May 2026.",
            "page_start": 1,
        }) + "\n",
        encoding="utf-8",
    )

    chunk_artifact = ArtifactRecord(
        artifact_id="art-chunk",
        project=ctx,
        kind="chunk",
        location="compiled/art-chunk.ndjson",
        content_hash="sha256:art-chunk",
        byte_size=100,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW, updated_at=_NOW,
        metadata={"run_id": "run-1"},
    )
    registry = _StubRegistry([chunk_artifact])

    workspace = MagicMock()
    workspace.area.return_value = tmp_path / "compiled"

    runner = DefaultValidationRunner(
        query_engine=MagicMock(),
        artifact_registry=registry,
        workspace=workspace,
    )

    retrieved = [RetrievedChunkRefDTO(
        artifact_id="art-chunk",
        chunk_id="ch-a", run_id="run-1", document_id="doc-1",
        source_location=None, score=0.5,
        preview="compiled/art-chunk.ndjson",
        artifact_kind="chunk",
    )]
    citations = [ValidationCitationDTO(
        artifact_id="art-chunk",
        artifact_type="chunk",
        chunk_id="ch-a", run_id="run-1",
        # preview NOT set — the runner should fill it in
    )]

    enriched = runner._enrich_citations_with_preview(  # noqa: SLF001
        ctx=ctx, retrieved=retrieved, citations=citations,
    )

    assert len(enriched) == 1
    assert enriched[0].preview is not None
    assert "20 May 2026" in enriched[0].preview


def test_runner_preserves_existing_preview_if_set(ctx, tmp_path: Path):
    """Already-populated preview is left alone (the enricher is
    additive, never destructive)."""
    from j1.validation.runner import DefaultValidationRunner

    workspace = MagicMock()
    workspace.area.return_value = tmp_path

    runner = DefaultValidationRunner(
        query_engine=MagicMock(),
        artifact_registry=_StubRegistry([]),
        workspace=workspace,
    )

    retrieved = [RetrievedChunkRefDTO(
        artifact_id="art-1",
        chunk_id=None, run_id="run-1", document_id="doc-1",
        source_location=None, score=0.5,
        preview="",
        artifact_kind="chunk",
    )]
    citations = [ValidationCitationDTO(
        artifact_id="art-1",
        artifact_type="chunk",
        preview="caller-supplied preview",
    )]

    enriched = runner._enrich_citations_with_preview(  # noqa: SLF001
        ctx=ctx, retrieved=retrieved, citations=citations,
    )

    assert enriched[0].preview == "caller-supplied preview"


def test_runner_noops_when_workspace_not_wired(ctx):
    """Legacy runners constructed without ``workspace`` keep working
    — they return citations unmodified. The previous-validation-
    style misconfiguration doesn't crash, just stays broken in
    the same way as before."""
    from j1.validation.runner import DefaultValidationRunner

    runner = DefaultValidationRunner(
        query_engine=MagicMock(),
        artifact_registry=_StubRegistry([]),
        # NO workspace
    )

    citations = [ValidationCitationDTO(
        artifact_id="art-1", artifact_type="chunk",
    )]
    enriched = runner._enrich_citations_with_preview(  # noqa: SLF001
        ctx=ctx, retrieved=[], citations=citations,
    )

    assert enriched == citations
    assert enriched[0].preview is None


# ---- _citation_to_dict propagates preview ------------------------


def test_citation_to_dict_propagates_preview():
    """The dict the judge consumes carries ``preview`` so the
    citation renderer can show body excerpts. Earlier the helper
    omitted preview, forcing every citation to render as
    lineage-only."""
    from j1.validation.checks import _citation_to_dict

    c = ValidationCitationDTO(
        artifact_id="art-1",
        artifact_type="chunk",
        preview="real body text",
    )
    d = _citation_to_dict(c)
    assert d["preview"] == "real body text"


def test_citation_to_dict_normalises_none_to_empty_string():
    """When preview is None, the dict still carries a string —
    keeps the judge prompt rendering simple."""
    from j1.validation.checks import _citation_to_dict

    c = ValidationCitationDTO(artifact_id="art-1", artifact_type="chunk")
    d = _citation_to_dict(c)
    assert d["preview"] == ""
