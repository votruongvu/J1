"""Tests for the graph_json skip rule in the evidence builder.

The validation answer-synthesizer is a TEXTUAL surface; raw graph
JSON / GraphML blobs are not usable evidence. RAGAnything's own
``aquery(mode="hybrid")`` path is the supported graph-aware QA
entry point — it walks LightRAG's entity/relation storage and
returns prose. Feeding raw graph_json to our local synthesizer was
the source of the "Graph QA contains JSON blocks but not enough
context" failure the validation report flagged.

These tests pin the rule so a future regression that re-introduces
graph_json into ``_KIND_PRIORITY`` (or removes it from
``_SKIP_KINDS``) fails loudly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactNotFoundError
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.validation.dtos import RetrievedChunkRefDTO
from j1.validation.evidence import build_evidence_blocks


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

    def list_artifacts(self, ctx, *, kind=None):  # noqa: ARG002
        if kind is None:
            return list(self._by_id.values())
        return [r for r in self._by_id.values() if r.kind == kind]


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


def _hit(*, artifact_id, kind, preview="", score=0.5):
    return RetrievedChunkRefDTO(
        artifact_id=artifact_id,
        chunk_id=None,
        run_id="run-1",
        document_id="doc-1",
        source_location=None,
        score=score,
        preview=preview,
        artifact_kind=kind,
    )


def test_graph_json_alone_yields_no_evidence(ctx, tmp_path: Path):
    """A retrieval with only graph_json hits returns zero evidence
    blocks — the synthesizer gets ``no_evidence`` and can correctly
    signal that the textual answer pipeline has nothing to ground in.
    Graph QA goes through RAGAnything.aquery, not through here."""
    artifact = _artifact(
        artifact_id="g-1",
        kind="graph_json",
        location="graph/g-1.json",
        ctx=ctx,
    )
    # Even though the file exists with realistic graph JSON, the
    # evidence builder must not read it.
    graph_path = tmp_path / "g-1.json"
    graph_path.write_text(
        json.dumps({
            "entities": [{"id": "e1", "name": "Project A"}],
            "relationships": [{"src": "e1", "tgt": "e2", "label": "owns"}],
        }),
        encoding="utf-8",
    )
    registry = _StubRegistry([artifact])

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[_hit(
            artifact_id="g-1",
            kind="graph_json",
            preview="graph_json/g-1",
        )],
        artifact_registry=registry,
        path_resolver=lambda r: graph_path,
    )

    assert blocks == []


def test_graph_json_skipped_even_when_mixed_with_chunks(ctx, tmp_path: Path):
    """Mixed retrieval (chunk + graph_json): only the chunk lands in
    evidence; graph_json drops out entirely regardless of score
    ranking. This is the production-shaped case — the engine often
    co-retrieves a high-scoring graph_json and a chunk for the same
    question."""
    chunk_art = _artifact(
        artifact_id="c-1", kind="chunk",
        location="chunks/c-1.ndjson", ctx=ctx,
    )
    chunk_path = tmp_path / "c-1.ndjson"
    chunk_path.write_text(
        json.dumps({
            "chunk_id": "chunk-a",
            "body": "The proposal due date is 20 May 2026.",
            "page_start": 3,
        }) + "\n",
        encoding="utf-8",
    )
    graph_art = _artifact(
        artifact_id="g-1", kind="graph_json",
        location="graph/g-1.json", ctx=ctx,
    )
    graph_path = tmp_path / "g-1.json"
    graph_path.write_text(json.dumps({"entities": []}), encoding="utf-8")
    registry = _StubRegistry([chunk_art, graph_art])

    def _resolve(record):
        return chunk_path if record.kind == "chunk" else graph_path

    blocks = build_evidence_blocks(
        ctx=ctx,
        retrieved=[
            # graph_json appears first to confirm ordering doesn't
            # leak it through — _SKIP_KINDS wins regardless.
            _hit(artifact_id="g-1", kind="graph_json", score=0.9),
            _hit(artifact_id="c-1", kind="chunk", score=0.5),
        ],
        artifact_registry=registry,
        path_resolver=_resolve,
    )

    assert len(blocks) == 1
    assert blocks[0].artifact_type == "chunk"
    assert "20 May 2026" in blocks[0].text


def test_skip_kinds_membership_for_graph_json():
    """Locked-in test on the constant itself: graph_json must be in
    ``_SKIP_KINDS`` and out of ``_KIND_PRIORITY``. A future PR that
    reintroduces graph_json as priority-50 evidence will fail this
    test loudly."""
    from j1.validation.evidence import _KIND_PRIORITY, _SKIP_KINDS

    assert "graph_json" in _SKIP_KINDS
    assert "graph_json" not in _KIND_PRIORITY
