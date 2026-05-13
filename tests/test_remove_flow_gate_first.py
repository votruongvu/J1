"""Tests for the gate-first Remove flow.

Spec rules:

  * Remove flips ``lifecycle_status="removing"`` + clears
    ``active_run_id`` + flips ``knowledge_state="removed"``
    BEFORE any destructive work — so the eligibility resolver
    immediately stops admitting the document's runs into
    queries.
  * Then runs ``cleanup_document`` synchronously: artifacts,
    FTS rows, workspace dirs, raw file all go away.
  * On success, ``lifecycle_status`` lands on ``removed``.
  * On partial failure, ``lifecycle_status`` lands on
    ``cleanup_failed`` so operators can spot the orphan.
  * Re-running Remove on an already-removed (or cleanup-failed)
    document is a no-op — no re-cleanup, no duplicate audit
    event.
"""

from __future__ import annotations

from datetime import datetime, timezone

from j1.artifacts.models import ArtifactRecord
from j1.documents.cleanup import CleanupResult, CleanupStepResult
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.intake.registry import JsonSourceRegistry
from j1.artifacts.registry import JsonArtifactRegistry
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.query.eligibility import resolve_eligible_active_run_ids
from j1.query.scope import ActiveScope, WorkspaceScope


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


class _SpyCleanup:
    """In-test stand-in for ``DocumentCleanupService``.

    Records every call + lets the test choose between ``ok=True``
    and a partial-failure outcome — so we can pin both the happy
    path and the ``cleanup_failed`` lifecycle branch. On
    ``ok=True``, it also deletes the document from the supplied
    registry (mirrors the real ``cleanup_document`` behaviour the
    second-call idempotence test relies on)."""

    def __init__(self, *, ok: bool = True, registry=None) -> None:
        self.calls: list[tuple] = []
        self._ok = ok
        self._registry = registry

    def cleanup_document(self, ctx, *, document_id):
        self.calls.append((ctx.project_id, document_id))
        if self._ok:
            if self._registry is not None:
                try:
                    self._registry.delete(ctx, document_id)
                except Exception:  # noqa: BLE001 — best-effort
                    pass
            return CleanupResult(
                ok=True,
                steps=[CleanupStepResult(name="artifacts", ok=True,
                                          items_removed=3)],
            )
        return CleanupResult(
            ok=False,
            steps=[
                CleanupStepResult(name="artifacts", ok=True),
                CleanupStepResult(
                    name="run_workspace",
                    ok=False,
                    error="rmtree failed",
                ),
            ],
        )


def _make_doc(registry, ctx, *, document_id="doc-1", active_run_id="run-A"):
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state="attached",
        active_run_id=active_run_id,
    )
    registry.add(record)
    return record


def _add_artifact(workspace, ctx, artifact_registry, *, run_id, artifact_id):
    from j1.workspace.layout import WorkspaceArea
    area_dir = workspace.area(ctx, WorkspaceArea.COMPILED)
    area_dir.mkdir(parents=True, exist_ok=True)
    (area_dir / f"{artifact_id}.txt").write_bytes(b"hello")
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"{WorkspaceArea.COMPILED.value}/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}",
        byte_size=5,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=["doc-1"],
        metadata={"run_id": run_id},
    )
    artifact_registry._raw_add(record)


def test_remove_flips_lifecycle_to_removed_and_clears_active_run(
    ctx, workspace,
):
    registry = JsonSourceRegistry(workspace)
    artifact_registry = JsonArtifactRegistry(workspace)
    _make_doc(registry, ctx)
    spy = _SpyCleanup(ok=True)
    service = DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        cleanup=spy,
    )

    final = service.remove(ctx, "doc-1", actor="op")
    assert final.knowledge_state == "removed"
    assert final.active_run_id is None
    assert final.lifecycle_status == "removed"
    assert final.removed_at is not None
    # Cleanup service was actually called.
    assert spy.calls == [(ctx.project_id, "doc-1")]


def test_remove_gate_takes_effect_before_cleanup(ctx, workspace):
    """The eligibility resolver must see ``run_ids=frozenset()`` for
    the document the moment the gate flips — BEFORE cleanup runs
    its first delete."""
    registry = JsonSourceRegistry(workspace)
    artifact_registry = JsonArtifactRegistry(workspace)
    _make_doc(registry, ctx)
    observed: list[frozenset] = []

    class _ObservingCleanup:
        def cleanup_document(self, ctx, *, document_id):
            # At this point the gate already flipped — eligibility
            # for both workspace and active scope must be empty.
            ws = resolve_eligible_active_run_ids(
                ctx=ctx, scope=WorkspaceScope(), registry=registry,
            )
            actv = resolve_eligible_active_run_ids(
                ctx=ctx,
                scope=ActiveScope(document_id=document_id),
                registry=registry,
            )
            observed.append(ws.run_ids)
            observed.append(actv.run_ids)
            return CleanupResult(ok=True, steps=[])

    service = DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        cleanup=_ObservingCleanup(),
    )

    service.remove(ctx, "doc-1", actor="op")
    assert observed == [frozenset(), frozenset()]


def test_remove_partial_failure_lands_in_cleanup_failed(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    artifact_registry = JsonArtifactRegistry(workspace)
    _make_doc(registry, ctx)
    spy = _SpyCleanup(ok=False)
    service = DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        cleanup=spy,
    )

    final = service.remove(ctx, "doc-1", actor="op")
    assert final.knowledge_state == "removed"
    assert final.lifecycle_status == "cleanup_failed"
    # Doc is still drop-from-queries — only the cleanup state
    # differs.
    eligible = resolve_eligible_active_run_ids(
        ctx=ctx, scope=WorkspaceScope(), registry=registry,
    )
    assert eligible.run_ids == frozenset()


def test_remove_is_idempotent_when_already_removed(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    artifact_registry = JsonArtifactRegistry(workspace)
    _make_doc(registry, ctx)
    spy = _SpyCleanup(ok=True)
    service = DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        cleanup=spy,
    )

    # Wire the spy to delete the doc on success — mirrors real
    # cleanup_document behaviour so the second remove() sees
    # "already gone".
    spy._registry = registry
    service.remove(ctx, "doc-1", actor="op")
    second = service.remove(ctx, "doc-1", actor="op")
    # Cleanup only ran once.
    assert len(spy.calls) == 1
    assert second.lifecycle_status == "removed"


def test_remove_after_cleanup_failed_does_not_retry(ctx, workspace):
    """If cleanup left orphans, the operator must explicitly handle
    them. Re-running Remove must NOT silently retry — it's a no-op
    until the operator unstuck the lifecycle_status."""
    registry = JsonSourceRegistry(workspace)
    artifact_registry = JsonArtifactRegistry(workspace)
    _make_doc(registry, ctx)
    spy = _SpyCleanup(ok=False)
    service = DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        cleanup=spy,
    )

    service.remove(ctx, "doc-1", actor="op")
    second = service.remove(ctx, "doc-1", actor="op")
    # Single cleanup invocation despite two remove() calls.
    assert len(spy.calls) == 1
    assert second.lifecycle_status == "cleanup_failed"


def test_remove_falls_back_to_legacy_when_no_cleanup_service(
    ctx, workspace,
):
    """When no cleanup service is wired (tests / legacy), Remove is
    the soft-tombstone behaviour from before the refactor:
    knowledge_state=removed, no cleanup run, lifecycle_status
    stays ``stable``."""
    registry = JsonSourceRegistry(workspace)
    artifact_registry = JsonArtifactRegistry(workspace)
    _make_doc(registry, ctx)
    service = DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
    )

    final = service.remove(ctx, "doc-1", actor="op")
    assert final.knowledge_state == "removed"
    assert final.lifecycle_status == "stable"  # untouched
