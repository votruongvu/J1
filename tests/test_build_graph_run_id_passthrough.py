"""Regression tests for ``run_id`` / ``document_id`` passthrough into
graph build.

Bug fixed:
   The production graph build path is
   ``ProjectProcessingWorkflow`` → ``ProcessingActivities.build_graph``
   → ``ProcessingService.build_graph`` → ``builder.build(ctx,
   artifact_ids)``. Previously ``ProcessingService.build_graph``
   accepted ``correlation_id`` (for audit) but did NOT forward it
   into ``builder.build()``. So ``RAGAnythingGraphBuilder.build()``
   defaulted ``run_id=None`` and ``document_id=None``, the
   ``RAGAnythingGraphRequest`` had no scope, and
   ``_graph_drafts_from_storage`` emitted drafts with empty
   ``metadata.run_id``. Even though registration would stamp
   ``run_id`` from ``correlation_id`` later, the drafts arrived at
   the registry with empty source bindings.

   The registry-level guard
   (``JsonArtifactRegistry.add`` →
   ``RegistryLineageError``) now catches this hermetically. These
   tests pin the upstream passthrough so the registry guard never
   has to fire in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from j1.processing.results import ArtifactProcessingResult, ResultStatus
from j1.projects.context import ProjectContext


@dataclass
class _Capture:
    ctx: ProjectContext | None = None
    artifact_ids: list[str] | None = None
    run_id: str | None = None
    document_id: str | None = None
    called: int = 0


class _SpyBuilder:
    """Stub graph builder that records the kwargs it received."""

    kind = "spy-graph"

    def __init__(self) -> None:
        self.capture = _Capture()

    def build(
        self,
        ctx: ProjectContext,
        artifact_ids: list[str],
        *,
        run_id: str | None = None,
        document_id: str | None = None,
    ) -> ArtifactProcessingResult:
        self.capture.ctx = ctx
        self.capture.artifact_ids = list(artifact_ids)
        self.capture.run_id = run_id
        self.capture.document_id = document_id
        self.capture.called += 1
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[],
        )


class _LegacySpyBuilder:
    """Stub builder whose ``build`` only takes
    ``(ctx, artifact_ids)`` — the Protocol minimum. Validates the
    inspect-based passthrough degrades gracefully."""

    kind = "legacy-spy-graph"

    def __init__(self) -> None:
        self.capture = _Capture()

    def build(
        self, ctx: ProjectContext, artifact_ids: list[str],
    ) -> ArtifactProcessingResult:
        self.capture.ctx = ctx
        self.capture.artifact_ids = list(artifact_ids)
        self.capture.called += 1
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=[],
        )


@pytest.fixture
def svc():
    """Build a minimal ProcessingService for direct .build_graph calls."""
    from datetime import datetime, timezone
    from pathlib import Path

    from j1.processing.service import ProcessingService

    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")
    return ProcessingService(
        workspace=workspace,
        artifact_registry=MagicMock(),
        audit=MagicMock(),
        cost=MagicMock(),
        clock=lambda: datetime(2026, 5, 13, tzinfo=timezone.utc),
        id_factory=lambda: "art-id",
    )


def test_build_graph_forwards_correlation_id_as_run_id(svc):
    """The headline fix: ``correlation_id`` reaches the builder as
    ``run_id``. The bridge uses it to scope LightRAG's workspace
    AND to stamp ``metadata.run_id`` on every emitted graph_json
    draft."""
    spy = _SpyBuilder()
    svc.build_graph(
        MagicMock(),  # ctx
        spy,
        ["chunk-1"],
        correlation_id="run-reindex-2",
        document_id="doc-a",
    )
    assert spy.capture.called == 1
    assert spy.capture.run_id == "run-reindex-2"
    assert spy.capture.document_id == "doc-a"
    assert spy.capture.artifact_ids == ["chunk-1"]


def test_build_graph_forwards_only_document_id_when_no_correlation_id(svc):
    """Half-scoped path: doc id present, no correlation id. The
    bridge still uses the doc id for the path namespace, and
    ``run_id`` stays None — registry guard then catches the orphan
    if the registration tries to land it."""
    spy = _SpyBuilder()
    svc.build_graph(
        MagicMock(), spy, [],
        correlation_id=None, document_id="doc-a",
    )
    assert spy.capture.run_id is None
    assert spy.capture.document_id == "doc-a"


def test_build_graph_passes_nothing_when_legacy_builder(svc):
    """Legacy builder whose ``build`` doesn't accept the kwargs →
    no TypeError. The inspect detection silently omits them. Same
    pre-fix behaviour, no regression."""
    spy = _LegacySpyBuilder()
    svc.build_graph(
        MagicMock(), spy, ["chunk-1"],
        correlation_id="run-1", document_id="doc-a",
    )
    assert spy.capture.called == 1
    assert spy.capture.artifact_ids == ["chunk-1"]


def test_orchestration_activity_threads_document_id():
    """The production ``ProcessingActivities.build_graph`` forwards
    ``input.document_id`` into ``ProcessingService.build_graph`` —
    pins the wiring at the activity layer."""
    from unittest.mock import MagicMock as _MM

    from j1.orchestration.activities.payloads import (
        GraphActivityInput,
        ProjectScope,
    )
    # The activity expects a ProcessingService instance; we use a
    # MagicMock and assert on the call kwargs. Build_graph is the
    # method that flows through to ``builder.build``.
    proc = _MM()
    proc.build_graph.return_value = ArtifactProcessingResult(
        status=ResultStatus.SUCCEEDED, drafts=[],
    )
    from j1.orchestration.activities.processing import ProcessingActivities

    acts = ProcessingActivities(
        processing=proc,
        sources=_MM(),
        artifacts=_MM(),
        compilers={},
        enrichers={},
        graph_builders={"spy-graph": _SpyBuilder()},
        indexers={},
        query_providers={},
    )

    acts.build_graph(GraphActivityInput(
        scope=ProjectScope(tenant_id="t1", project_id="p1"),
        artifact_ids=["chunk-1"],
        processor_kind="spy-graph",
        correlation_id="run-2",
        document_id="doc-a",
    ))

    proc.build_graph.assert_called_once()
    _, kwargs = proc.build_graph.call_args
    assert kwargs["correlation_id"] == "run-2"
    assert kwargs["document_id"] == "doc-a"
