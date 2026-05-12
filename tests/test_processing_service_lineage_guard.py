"""Tests for the lineage guard inside
``j1.processing.service.ProcessingService._register_draft``.

The legacy ProcessingService path now uses a **two-policy** guard
(by kind):

  * ``graph_json`` — **fail-fast**. Graph artifacts are the
    production failure mode the latest validation report flagged
    (7 graph_json rows with ``run_id=None`` leaking into the
    index). Producers MUST stamp run_id at the draft layer
    (``_graph_drafts_from_storage``). Missing run_id raises
    ``LineageError`` so the bug surfaces immediately.

  * All other lineage-required kinds (``chunk``, ``compiled.text``,
    ``enriched.*``, ``parsed_content_manifest``, …) — **soft
    guard**. Emits a WARNING and tags
    ``lineage_origin=legacy_processing_service`` so the
    project-wide cleanup sweep can find them. Legacy test fixtures
    and direct adapter callers that legitimately bypass
    orchestration keep working.

The orchestration path (``_materialize_draft``) remains the strict
gate for ALL lineage-required kinds — it raises ``LineageError``
regardless of kind. This module's tests target the legacy path.
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


def _build_svc(artifacts=None):
    """Build a minimal ProcessingService — only the registration
    plumbing matters; everything else is mocked."""
    from datetime import datetime, timezone
    from pathlib import Path
    from unittest.mock import MagicMock

    from j1.processing.service import ProcessingService

    artifacts = artifacts or MagicMock()
    if not hasattr(artifacts, "add"):
        artifacts.add = MagicMock()
    workspace = MagicMock()
    tmp_area = Path("/tmp/_test_proc_svc_lineage")
    tmp_area.mkdir(parents=True, exist_ok=True)
    workspace.area.return_value = tmp_area

    return ProcessingService(
        workspace=workspace,
        artifact_registry=artifacts,
        audit=MagicMock(),
        cost=MagicMock(),
        clock=lambda: datetime(2026, 5, 12, tzinfo=timezone.utc),
        id_factory=lambda: "test-art-id",
    )


def test_graph_json_without_run_id_raises_lineage_error():
    """The headline regression: ``graph_json`` registration MUST
    fail-fast when run_id is missing. This is the production failure
    mode operators hit (7 graph_json rows with run_id=None leaking
    into the index)."""
    from unittest.mock import MagicMock

    from j1.processing.results import ArtifactDraft
    from j1.processing.service import LineageError
    from j1.workspace.layout import WorkspaceArea

    svc = _build_svc()
    draft = ArtifactDraft(
        kind="graph_json",
        suggested_extension=".json",
        content=b'{"entities": []}',
        source_document_ids=[],
        source_artifact_ids=[],
        metadata={},
        review_required=False,
    )

    import pytest

    with pytest.raises(LineageError) as exc_info:
        svc._register_draft(  # noqa: SLF001
            ctx=MagicMock(),
            draft=draft,
            area=WorkspaceArea.GRAPH,
            fallback_source_documents=[],
            fallback_source_artifacts=[],
            run_id=None,
        )
    # Error message must point the operator at the producer.
    assert "graph_json" in str(exc_info.value)
    assert "run_id" in str(exc_info.value)


def test_graph_json_with_run_id_succeeds():
    """Counter-example: stamping run_id (either explicitly via the
    correlation_id parameter OR in ``draft.metadata`` directly) is
    sufficient. The new ``_graph_drafts_from_storage`` always
    stamps the metadata key, so this is the happy path."""
    from unittest.mock import MagicMock

    from j1.processing.results import ArtifactDraft
    from j1.workspace.layout import WorkspaceArea

    svc = _build_svc()
    draft = ArtifactDraft(
        kind="graph_json",
        suggested_extension=".json",
        content=b'{"entities": []}',
        source_document_ids=[],
        source_artifact_ids=[],
        metadata={"run_id": "run-x"},  # stamped at draft layer
        review_required=False,
    )

    record = svc._register_draft(  # noqa: SLF001
        ctx=MagicMock(),
        draft=draft,
        area=WorkspaceArea.GRAPH,
        fallback_source_documents=[],
        fallback_source_artifacts=[],
        run_id=None,  # not passed at registration; draft already has it
    )
    assert record.metadata["run_id"] == "run-x"


def test_legacy_path_logs_warning_for_non_graph_kind_missing_run_id(caplog):
    """Non-graph_json kinds (chunk, compiled.text, enriched.*) keep
    the soft-guard behaviour — WARNING + tag instead of raise. This
    preserves the legacy test fixtures and direct adapter callers
    that legitimately bypass orchestration."""
    from unittest.mock import MagicMock

    from j1.processing.results import ArtifactDraft
    from j1.workspace.layout import WorkspaceArea

    svc = _build_svc()
    draft = ArtifactDraft(
        kind="chunk",  # lineage-required but NOT graph_json
        suggested_extension=".ndjson",
        content=b'{"chunk_id":"c1","body":"x"}\n',
        source_document_ids=[],
        source_artifact_ids=[],
        metadata={},
        review_required=False,
    )

    with caplog.at_level(logging.WARNING, logger="j1.processing.service"):
        record = svc._register_draft(  # noqa: SLF001
            ctx=MagicMock(),
            draft=draft,
            area=WorkspaceArea.COMPILED,
            fallback_source_documents=[],
            fallback_source_artifacts=[],
            run_id=None,
        )

    assert any("without run_id" in r.message for r in caplog.records)
    assert record.metadata.get("lineage_origin") == "legacy_processing_service"
