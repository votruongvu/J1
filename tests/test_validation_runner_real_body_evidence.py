"""Regression test for the validation runner loading REAL chunk
body text into the synthesizer prompt (not artifact titles).

Bug fixed:
   ``DefaultValidationRunner._maybe_synthesize_for_case`` built
   evidence blocks using ``RetrievedChunkRefDTO.preview`` as the
   text body. But ``preview`` is the engine's title-only
   summary (e.g. ``"compiled.text/b7e57…"``), NOT the artifact's
   actual content. So the LLM saw a list of titles + IDs and
   correctly replied "Not in the retrieved evidence." even when
   real text was retrievable. The manual-query path used
   ``build_evidence_blocks`` (which loads real bodies); the
   runner now uses the same helper.
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
from j1.query.models import QueryResponse, SourceReference


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
        if kind is None:
            return list(self._by_id.values())
        return [r for r in self._by_id.values() if r.kind == kind]


class _SpySynthesizer:
    """Captures the evidence the runner sends — so the test can
    assert real body text reaches the LLM."""

    def __init__(self):
        self.captured_evidence = None

    def synthesize(self, *, question, evidence):
        from j1.validation.synthesis import SynthesisResult
        self.captured_evidence = list(evidence)
        return SynthesisResult(
            answer="(stub)",
            provider="stub", model="stub-model",
            latency_ms=0, prompt_tokens=0, completion_tokens=0,
            error=None,
        )


def test_runner_loads_real_chunk_body_into_evidence(tmp_path: Path, ctx):
    """Headline: a retrieved chunk's body lands in the LLM prompt
    as real text, not as the artifact title.

    Replays the exact path that broke in production: retrieved chunk
    → evidence block → synthesizer call. Asserts on the evidence
    seen by the synthesizer stub."""
    from j1.validation.runner import DefaultValidationRunner
    from j1.workspace.layout import WorkspaceArea

    # Stage a chunk NDJSON file on disk.
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

    # Workspace resolver returns the staged dir for the compiled area.
    workspace = MagicMock()
    workspace.area.return_value = tmp_path / "compiled"

    # Engine returns a single chunk source — same shape as production
    # GraphQueryProvider / KnowledgeQueryProvider after the
    # ``_record_to_source`` / ``_hit_to_source`` projections.
    engine = MagicMock()
    engine.query.return_value = QueryResponse(
        answer="(engine answer)",
        mode_used="knowledge_first",
        sources=[SourceReference(
            artifact_id="art-chunk",
            artifact_type="chunk",
            title="compiled/art-chunk.ndjson",  # ← title, NOT body
            chunk_id="ch-a",
            run_id="run-1",
        )],
    )

    spy_synth = _SpySynthesizer()
    runner = DefaultValidationRunner(
        query_engine=engine,
        artifact_registry=registry,
        answer_synthesizer=spy_synth,
        workspace=workspace,
    )

    # Use the internal entry point — we only care about the
    # evidence-building behaviour, not the full run-checks pipeline.
    from j1.validation.dtos import (
        RetrievedChunkRefDTO, ValidationTestCaseDTO,
    )
    case = ValidationTestCaseDTO(
        test_case_id="tc-1",
        question="What is the proposal due date?",
        type="retrieval",
        priority="normal",
        expected_behavior="must_answer",
    )
    retrieved = [RetrievedChunkRefDTO(
        artifact_id="art-chunk",
        chunk_id="ch-a",
        run_id="run-1",
        document_id="doc-1",
        source_location=None,
        score=0.5,
        preview="compiled/art-chunk.ndjson",  # ← engine's title-only
        artifact_kind="chunk",
    )]

    runner._maybe_synthesize_for_case(  # noqa: SLF001 — direct test
        ctx=ctx, case=case, retrieved=retrieved,
    )

    assert spy_synth.captured_evidence is not None
    assert len(spy_synth.captured_evidence) == 1
    block = spy_synth.captured_evidence[0]
    # The real body, NOT the title, reached the LLM.
    assert "20 May 2026" in block.text
    assert "art-chunk" not in block.text  # no leftover title
    assert block.chunk_id == "ch-a"


def test_runner_falls_back_to_preview_when_workspace_not_wired(ctx):
    """Backward-compat: legacy callers that didn't pass a workspace
    keep the old title-only behaviour (with a WARNING log). The
    fix is opt-in via wiring — but the production path
    (`IngestionValidationService`) now always passes workspace."""
    import logging

    from j1.validation.runner import DefaultValidationRunner
    from j1.validation.dtos import (
        RetrievedChunkRefDTO, ValidationTestCaseDTO,
    )

    engine = MagicMock()
    spy_synth = _SpySynthesizer()
    runner = DefaultValidationRunner(
        query_engine=engine,
        artifact_registry=MagicMock(),
        answer_synthesizer=spy_synth,
        # NO workspace — legacy
    )
    case = ValidationTestCaseDTO(
        test_case_id="tc-1",
        question="anything?",
        type="retrieval", priority="normal",
        expected_behavior="must_answer",
    )
    retrieved = [RetrievedChunkRefDTO(
        artifact_id="art-1", chunk_id=None,
        run_id="run-1", document_id="doc-1",
        source_location=None, score=0.5,
        preview="some legacy preview text",
        artifact_kind="chunk",
    )]

    runner._maybe_synthesize_for_case(  # noqa: SLF001
        ctx=ctx, case=case, retrieved=retrieved,
    )

    # Fallback: evidence text IS the preview, same broken shape as
    # before this fix. Production wiring catches this via WARNING.
    assert spy_synth.captured_evidence[0].text == "some legacy preview text"
