"""Tests for the lineage telemetry guard inside
``j1.processing.service.ProcessingService._register_draft``.

The orchestration ``_materialize_draft`` path enforces
``metadata.run_id`` strictly for lineage-required kinds (raises
``LineageError`` outright). The legacy ``ProcessingService`` path
predates that contract and is still used by tests / direct adapter
calls that legitimately register artifacts without a run scope —
so blocking there would break existing flows.

Instead the legacy path logs a WARNING and tags the metadata with
``lineage_origin=legacy_processing_service`` so the project-wide
``invalidate_lineage_missing_artifacts`` sweep can clean up the
resulting orphans on demand.

These tests pin the catalogue + the soft-guard contract.
"""

from __future__ import annotations

import logging

from j1.processing.service import LineageError, _LINEAGE_REQUIRED_KINDS


def test_lineage_required_kinds_includes_graph_json():
    """graph_json is the canonical case operators hit in production —
    a typo or rename in the kind set would silently re-open the
    failure mode."""
    assert "graph_json" in _LINEAGE_REQUIRED_KINDS
    assert "chunk" in _LINEAGE_REQUIRED_KINDS
    assert "compiled.text" in _LINEAGE_REQUIRED_KINDS


def test_lineage_required_kinds_excludes_freeform_kinds():
    """Operator-uploaded blob kinds legitimately have no run_id and
    must NOT be in the required set; otherwise every upload would
    fail registration."""
    assert "raw_upload" not in _LINEAGE_REQUIRED_KINDS
    assert "user_attachment" not in _LINEAGE_REQUIRED_KINDS


def test_lineage_error_is_runtime_error_subclass():
    """The orchestration path raises ``LineageError`` — tests that
    catch either should use ``RuntimeError`` to remain decoupled."""
    assert issubclass(LineageError, RuntimeError)


def test_legacy_path_logs_warning_on_missing_run_id(caplog):
    """Direct ``_register_draft`` callers without ``run_id`` see a
    WARNING in logs (and the artifact lands with a tagged metadata
    field so the cleanup sweep can find it later). This is the soft
    guard for legacy/test paths — the orchestration path remains the
    strict gate."""
    from datetime import datetime, timezone
    from pathlib import Path
    from unittest.mock import MagicMock

    from j1.processing.results import ArtifactDraft
    from j1.processing.service import ProcessingService
    from j1.workspace.layout import WorkspaceArea

    # Minimal ProcessingService — only the registration plumbing
    # matters for this test. Use MagicMock for everything else.
    artifacts = MagicMock()
    artifacts.add = MagicMock()
    workspace = MagicMock()
    tmp_area = Path("/tmp/_test_proc_svc_lineage")
    tmp_area.mkdir(parents=True, exist_ok=True)
    workspace.area.return_value = tmp_area

    svc = ProcessingService(
        workspace=workspace,
        artifact_registry=artifacts,
        audit=MagicMock(),
        cost=MagicMock(),
        clock=lambda: datetime(2026, 5, 12, tzinfo=timezone.utc),
        id_factory=lambda: "test-art-id",
    )

    draft = ArtifactDraft(
        kind="graph_json",
        suggested_extension=".json",
        content=b'{"entities": []}',
        source_document_ids=[],
        source_artifact_ids=[],
        metadata={},
        review_required=False,
    )

    with caplog.at_level(logging.WARNING, logger="j1.processing.service"):
        record = svc._register_draft(  # noqa: SLF001 — exercising private path on purpose
            ctx=MagicMock(),
            draft=draft,
            area=WorkspaceArea.GRAPH,
            fallback_source_documents=[],
            fallback_source_artifacts=[],
            run_id=None,
        )

    # The WARNING fired, the artifact still landed, and the metadata
    # carries the tag the cleanup sweep can match on.
    assert any(
        "without run_id" in r.message for r in caplog.records
    )
    assert record.metadata.get("lineage_origin") == "legacy_processing_service"
    assert artifacts.add.called
