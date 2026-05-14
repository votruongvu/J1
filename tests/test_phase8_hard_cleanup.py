"""Phase 8 hard-cleanup invariants.

These tests pin the Phase 8 retirements:

* ``SqliteSearchIndexer`` is GONE — no longer importable from
  ``j1.search.indexer``.
* ``SqliteEvidenceAdapter`` is GONE — no longer importable from
  ``j1.search.evidence_adapter``.
* ``select_evidence_adapter`` rejects every backend except
  ``postgres_fts``.
* ``EvidenceBackend`` enum has only ``POSTGRES_FTS``.
* ``DocumentRecord.active_run_id`` is no longer a load-bearing
  field — promotion does NOT write it.
* ``try_promote_active_run_id`` is GONE from the source registry.
* ``DocumentCleanupService.cleanup_run`` is a no-op shim.
* ``J1_LEGACY_SQLITE_EVIDENCE_ENABLED`` env flag does nothing —
  the legacy SQLite dual-write path was deleted.
"""

from __future__ import annotations

import pytest


def test_sqlite_search_indexer_class_is_deleted():
    from j1.search import indexer
    assert not hasattr(indexer, "SqliteSearchIndexer")


def test_sqlite_evidence_adapter_class_is_deleted():
    from j1.search import evidence_adapter
    assert not hasattr(evidence_adapter, "SqliteEvidenceAdapter")


def test_top_level_module_no_longer_re_exports_sqlite():
    import j1
    assert not hasattr(j1, "SqliteSearchIndexer")
    assert not hasattr(j1, "DEFAULT_DB_FILENAME")
    assert not hasattr(j1, "MAX_INDEXED_BYTES")


def test_evidence_backend_enum_has_only_postgres_fts():
    from j1.config.runtime import EvidenceBackend
    members = {m.value for m in EvidenceBackend}
    assert members == {"postgres_fts"}


def test_select_evidence_adapter_rejects_sqlite_backend():
    from j1.search.evidence_adapter import select_evidence_adapter
    with pytest.raises(ValueError, match="unsupported evidence backend"):
        select_evidence_adapter("sqlite_fts5")


def test_source_registry_has_no_try_promote_active_run_id():
    """Phase 8: the run-id CAS promotion API is gone. Only
    ``try_promote_active_snapshot_id`` remains."""
    from j1.intake.registry import JsonSourceRegistry, SourceRegistry
    assert not hasattr(JsonSourceRegistry, "try_promote_active_run_id")
    # Protocol also doesn't advertise it anymore.
    assert "try_promote_active_run_id" not in dir(SourceRegistry)


def test_cleanup_run_is_no_op_shim(tmp_path):
    """Phase 8: ``cleanup_run`` survives only as a backward-compat
    shim that does NOTHING. Run-scoped cleanup was deleted; the
    canonical API is ``cleanup_snapshot``."""
    from j1.config.settings import Settings
    from j1.documents.cleanup import DocumentCleanupService, CleanupResult
    from j1.projects.context import ProjectContext
    from j1.workspace.resolver import WorkspaceResolver

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    svc = DocumentCleanupService(workspace=workspace)
    result = svc.cleanup_run(
        ProjectContext(tenant_id="t", project_id="p", profile=None),
        document_id="doc-1", run_id="run-1",
    )
    assert isinstance(result, CleanupResult)
    assert result.ok is True
    assert result.steps == []


def test_legacy_sqlite_env_flag_no_longer_does_anything(monkeypatch):
    """Phase 8: ``J1_LEGACY_SQLITE_EVIDENCE_ENABLED`` was deleted.
    Setting it doesn't enable any legacy code path because the
    SQLite dispatch in ``SearchActivities`` is gone."""
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.config.settings import Settings
    from j1.orchestration.activities.search import SearchActivities
    from j1.workspace.resolver import WorkspaceResolver
    from pathlib import Path
    import tempfile

    monkeypatch.setenv("J1_LEGACY_SQLITE_EVIDENCE_ENABLED", "true")
    with tempfile.TemporaryDirectory() as tmp:
        ws = WorkspaceResolver(Settings(data_root=Path(tmp)))
        audit = DefaultAuditRecorder(JsonlAuditSink(ws))
        # Constructor no longer accepts ``legacy_sqlite_enabled``.
        sa = SearchActivities(audit=audit)
        assert not hasattr(sa, "_legacy_sqlite_enabled")


def test_document_record_active_run_id_is_deleted():
    """Phase 9: ``active_run_id`` is REMOVED from ``DocumentRecord``.
    ``active_snapshot_id`` is the canonical visibility key."""
    import dataclasses
    from datetime import datetime, timezone
    from j1.documents.models import DocumentRecord
    from j1.jobs.status import ProcessingStatus
    from j1.projects.context import ProjectContext

    fields = {f.name for f in dataclasses.fields(DocumentRecord)}
    assert "active_run_id" not in fields
    assert "active_snapshot_id" in fields

    doc = DocumentRecord(
        document_id="d-1",
        project=ProjectContext(tenant_id="t", project_id="p", profile=None),
        original_filename="x.pdf", stored_filename="x.pdf",
        mime_type="application/pdf", file_size=1, checksum="sha:x",
        status=ProcessingStatus.SUCCEEDED,
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert doc.active_snapshot_id is None


def test_postgres_fts_is_only_evidence_backend():
    """Phase 8: ``postgres_fts`` is the only supported value. The
    runtime config loader rejects any other backend at parse time."""
    from j1.config.runtime import EvidenceBackend, load_runtime_config
    from j1.errors.exceptions import ConfigError

    cfg = load_runtime_config({"J1_EVIDENCE_BACKEND": "postgres_fts"})
    assert cfg.evidence.backend == EvidenceBackend.POSTGRES_FTS

    with pytest.raises(ConfigError):
        load_runtime_config({"J1_EVIDENCE_BACKEND": "sqlite_fts5"})
