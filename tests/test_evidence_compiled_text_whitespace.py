"""Test that ``_load_compiled_text_window`` normalises PDF-style
whitespace before handing text to the synthesizer.

PDF compilers emit text with collapsed/duplicated whitespace and
soft line breaks (e.g. ``"due\\n  20  May  2026"``). Without
normalisation:

  1. The synthesizer's prompt looks visually broken.
  2. The grounding judge (which already normalises) flags otherwise-
     valid answers as unsupported on cosmetic grounds.
  3. The dedup prefix becomes whitespace-dominated, causing
     near-identical chunks to leak past the dedup gate.

This test pins the whitespace normalisation step so a future
refactor that bypasses it triggers a clear failure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactNotFoundError
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.validation.dtos import RetrievedChunkRefDTO
from j1.validation.evidence import (
    _normalise_pdf_whitespace,
    build_evidence_blocks,
)


_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


class _StubRegistry:
    def __init__(self, records):
        self._by_id = {r.artifact_id: r for r in records}

    def get(self, ctx, artifact_id):  # noqa: ARG002
        if artifact_id not in self._by_id:
            raise ArtifactNotFoundError(artifact_id)
        return self._by_id[artifact_id]


def _artifact(*, artifact_id, kind, location, ctx):
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=100,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_normalise_pdf_whitespace_collapses_runs():
    assert _normalise_pdf_whitespace("due\n  20  May  2026") == "due 20 May 2026"
    assert _normalise_pdf_whitespace("a\n\n\nb") == "a b"
    assert _normalise_pdf_whitespace("\t\t hello\nworld \t") == "hello world"


def test_normalise_pdf_whitespace_preserves_punctuation():
    """Only whitespace collapses — punctuation, casing, and unicode
    are left intact."""
    assert _normalise_pdf_whitespace("Section\n 3.1: Scope.") == "Section 3.1: Scope."
    assert _normalise_pdf_whitespace("résumé—2026") == "résumé—2026"


def test_normalise_pdf_whitespace_handles_empty_and_none():
    assert _normalise_pdf_whitespace("") == ""
    assert _normalise_pdf_whitespace("   ") == ""
    assert _normalise_pdf_whitespace(None) == ""  # type: ignore[arg-type]


def test_compiled_text_evidence_block_has_normalised_whitespace(
    tmp_path: Path, ctx,
):
    """End-to-end: a compiled.text artifact with PDF-style whitespace
    on disk produces a normalised evidence block. This is the
    failure mode the validation report flagged — answers wouldn't
    match the question because the chunk text was visually broken."""
    artifact = _artifact(
        artifact_id="ct-1", kind="compiled.text",
        location="compiled/ct-1.txt", ctx=ctx,
    )
    text_path = tmp_path / "ct-1.txt"
    # Mimic a real PDF compile output — multiple lines with extra
    # spaces and a soft line break inside a date.
    text_path.write_text(
        "Section\n  3.1   Scope\n\n"
        "The   proposal  due\n  date is  20  May  2026.\n",
        encoding="utf-8",
    )
    registry = _StubRegistry([artifact])

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[RetrievedChunkRefDTO(
            artifact_id="ct-1",
            chunk_id=None,
            run_id="run-1",
            document_id="doc-1",
            source_location=None,
            score=0.5,
            preview="",
            artifact_kind="compiled.text",
        )],
        artifact_registry=registry,
        path_resolver=lambda r: text_path,
    )

    assert len(blocks) == 1
    text = blocks[0].text
    # Normalised: every whitespace run is a single space, no \n's.
    assert "\n" not in text
    assert "  " not in text
    # Content preserved: every token from the original is still
    # present, in order.
    assert "Section 3.1 Scope" in text
    assert "20 May 2026" in text
