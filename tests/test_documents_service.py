"""Tests for `DocumentLifecycleService` (Phase 3).

The service is the layer that activates Phase 2's retrieval filter:
calling `detach()` should make the document's artifacts disappear
from retrieval; `attach()` should bring them back; `remove()` should
make the disappearance permanent (artifacts disowned by the
knowledge layer until re-upload).

Test coverage prioritises the contract — what state the data is in
after each transition — and the idempotency rules the FE relies on.
Audit emission is tested via a stub recorder because the real audit
path has its own dedicated tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactNotFoundError, ArtifactRegistry
from j1.documents.lifecycle import filter_to_attached_artifacts
from j1.documents.models import DocumentRecord
from j1.documents.service import (
    DocumentLifecycleError,
    DocumentLifecycleService,
)
from j1.errors.exceptions import DocumentNotFoundError
from j1.intake.registry import JsonSourceRegistry
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


# ---- Test doubles ----------------------------------------------------


class _InMemoryArtifactRegistry:
    """Test fixture matching `ArtifactRegistry` shape just enough for
 the service. Real `JsonArtifactRegistry` would also work but
 needs a workspace; in-memory keeps these tests file-system-free
 + fast."""

    def __init__(self) -> None:
        self._records: list[ArtifactRecord] = []

    def add(self, record: ArtifactRecord) -> None:
        self._records.append(record)

    def get(self, ctx: ProjectContext, artifact_id: str) -> ArtifactRecord:
        for r in self._records:
            if r.artifact_id == artifact_id:
                return r
        raise ArtifactNotFoundError(artifact_id)

    def list_artifacts(self, ctx, *, kind=None):
        if kind is None:
            return list(self._records)
        return [r for r in self._records if r.kind == kind]

    def update_metadata(self, ctx, artifact_id, metadata):
        from dataclasses import replace
        for i, r in enumerate(self._records):
            if r.artifact_id == artifact_id:
                self._records[i] = replace(r, metadata=dict(metadata))
                return
        raise ArtifactNotFoundError(artifact_id)

    def find_by_content_hash(self, ctx, content_hash):
        return None  # unused in these tests


class _CapturingAudit:
    """Records every audit emission for assertion. Pluggable into
 the service's `audit=` slot."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, ctx, *, actor, action, target_kind, target_id, payload=None, correlation_id=None):
        self.events.append({
            "actor": actor,
            "action": action,
            "target_kind": target_kind,
            "target_id": target_id,
            "payload": dict(payload or {}),
        })
        return "audit-id"


# ---- Builders -----------------------------------------------------


def _document(
    *, ctx: ProjectContext, document_id: str = "doc-1",
    state: str = "attached",
) -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename="bridge.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state=state,  # type: ignore[arg-type]
        active_run_id="r-1",
    )


def _artifact(
    *, ctx: ProjectContext, artifact_id: str, document_id: str,
    metadata: dict | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"compiled/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}",
        byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=[document_id],
        source_artifact_ids=[],
        metadata=metadata or {},
    )


@pytest.fixture
def service(workspace, ctx, registry):
    """Service wired against the real JsonSourceRegistry + an
 in-memory artifact registry. Audit is captured so tests can
 assert event payloads."""
    artifacts = _InMemoryArtifactRegistry()
    audit = _CapturingAudit()
    svc = DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifacts,
        audit=audit,
        clock=lambda: _NOW,
    )
    return svc, registry, artifacts, audit


# ---- Detach -------------------------------------------------------


def test_detach_flips_document_state_and_stamps_artifacts(service, ctx):
    svc, registry, artifacts, audit = service
    registry.add(_document(ctx=ctx, document_id="doc-1"))
    artifacts.add(_artifact(ctx=ctx, artifact_id="a-1", document_id="doc-1"))
    artifacts.add(_artifact(ctx=ctx, artifact_id="a-2", document_id="doc-1"))
    artifacts.add(_artifact(ctx=ctx, artifact_id="a-3", document_id="doc-other"))

    updated = svc.detach(ctx, "doc-1", actor="alice")

    assert updated.knowledge_state == "detached"
    assert updated.updated_at == _NOW
    # All matching artifacts now carry knowledge_state="detached".
    a1 = artifacts.get(ctx, "a-1")
    a2 = artifacts.get(ctx, "a-2")
    a3 = artifacts.get(ctx, "a-3")
    assert a1.metadata["knowledge_state"] == "detached"
    assert a2.metadata["knowledge_state"] == "detached"
    # Untargeted artifact (different document) MUST stay clean.
    assert "knowledge_state" not in a3.metadata
    # Retrieval filter now drops the detached artifacts.
    visible = filter_to_attached_artifacts([a1, a2, a3])
    assert {a.artifact_id for a in visible} == {"a-3"}


def test_detach_emits_audit_event_with_state_payload(service, ctx):
    svc, registry, artifacts, audit = service
    registry.add(_document(ctx=ctx, document_id="doc-1"))
    artifacts.add(_artifact(ctx=ctx, artifact_id="a-1", document_id="doc-1"))

    svc.detach(ctx, "doc-1", actor="alice")

    assert len(audit.events) == 1
    ev = audit.events[0]
    assert ev["action"] == "j1.document.detached"
    assert ev["target_kind"] == "document"
    assert ev["target_id"] == "doc-1"
    assert ev["actor"] == "alice"
    assert ev["payload"]["previous_state"] == "attached"
    assert ev["payload"]["new_state"] == "detached"
    assert ev["payload"]["stamped_artifact_count"] == 1


def test_detach_is_idempotent(service, ctx):
    """Repeating detach on an already-detached document is a no-op:
 no audit event, no extra artifact writes. The FE can safely
 retry a click on a stale UI without flooding the audit log."""
    svc, registry, artifacts, audit = service
    registry.add(_document(ctx=ctx, document_id="doc-1", state="detached"))
    artifacts.add(_artifact(
        ctx=ctx, artifact_id="a-1", document_id="doc-1",
        metadata={"knowledge_state": "detached"},
    ))

    svc.detach(ctx, "doc-1", actor="alice")
    assert audit.events == []


# ---- Attach -------------------------------------------------------


def test_attach_brings_detached_document_back(service, ctx):
    svc, registry, artifacts, audit = service
    registry.add(_document(ctx=ctx, document_id="doc-1", state="detached"))
    artifacts.add(_artifact(
        ctx=ctx, artifact_id="a-1", document_id="doc-1",
        metadata={"knowledge_state": "detached"},
    ))

    updated = svc.attach(ctx, "doc-1", actor="alice")

    assert updated.knowledge_state == "attached"
    a1 = artifacts.get(ctx, "a-1")
    assert a1.metadata["knowledge_state"] == "attached"
    # Filter now sees it again.
    assert filter_to_attached_artifacts([a1]) == [a1]


def test_attach_is_idempotent_on_already_attached(service, ctx):
    svc, registry, _, audit = service
    registry.add(_document(ctx=ctx, document_id="doc-1", state="attached"))
    svc.attach(ctx, "doc-1", actor="alice")
    assert audit.events == []


def test_attach_rejects_removed_document(service, ctx):
    """Removed is a one-way terminal state for the knowledge layer.
 Re-attaching after remove requires re-upload — otherwise we'd
 silently bring back artifacts whose lineage the system has
 already disowned."""
    svc, registry, _, _ = service
    registry.add(_document(ctx=ctx, document_id="doc-1", state="removed"))
    with pytest.raises(DocumentLifecycleError, match="re-upload"):
        svc.attach(ctx, "doc-1", actor="alice")


# ---- Remove ------------------------------------------------------


def test_remove_disowns_document_and_clears_active_run(service, ctx):
    svc, registry, artifacts, audit = service
    registry.add(_document(ctx=ctx, document_id="doc-1"))
    artifacts.add(_artifact(ctx=ctx, artifact_id="a-1", document_id="doc-1"))

    updated = svc.remove(ctx, "doc-1", actor="bob")

    assert updated.knowledge_state == "removed"
    assert updated.removed_at == _NOW
    assert updated.active_run_id is None  # cleared on remove
    a1 = artifacts.get(ctx, "a-1")
    assert a1.metadata["knowledge_state"] == "removed"
    assert audit.events[0]["action"] == "j1.document.removed"


def test_remove_then_detach_is_rejected(service, ctx):
    svc, registry, _, _ = service
    registry.add(_document(ctx=ctx, document_id="doc-1"))
    svc.remove(ctx, "doc-1", actor="bob")
    with pytest.raises(DocumentLifecycleError):
        svc.detach(ctx, "doc-1", actor="bob")


def test_remove_is_idempotent(service, ctx):
    svc, registry, _, audit = service
    registry.add(_document(ctx=ctx, document_id="doc-1", state="removed"))
    svc.remove(ctx, "doc-1", actor="bob")
    assert audit.events == []


# ---- Missing-document handling ---------------------------------


def test_attach_raises_for_unknown_document(service, ctx):
    svc, *_ = service
    with pytest.raises(DocumentNotFoundError):
        svc.attach(ctx, "missing", actor="alice")


def test_detach_raises_for_unknown_document(service, ctx):
    svc, *_ = service
    with pytest.raises(DocumentNotFoundError):
        svc.detach(ctx, "missing", actor="alice")


def test_remove_raises_for_unknown_document(service, ctx):
    svc, *_ = service
    with pytest.raises(DocumentNotFoundError):
        svc.remove(ctx, "missing", actor="alice")


# ---- Artifact stamping edge cases ---------------------------------


def test_detach_skips_artifacts_belonging_to_other_documents(service, ctx):
    """Only artifacts whose source_document_ids include THIS doc get
 stamped. Multi-doc artifacts and artifacts for other documents
 stay clean."""
    svc, registry, artifacts, audit = service
    registry.add(_document(ctx=ctx, document_id="doc-1"))
    artifacts.add(_artifact(ctx=ctx, artifact_id="mine", document_id="doc-1"))
    other = _artifact(ctx=ctx, artifact_id="theirs", document_id="doc-2")
    artifacts.add(other)

    svc.detach(ctx, "doc-1", actor="alice")
    assert "knowledge_state" not in artifacts.get(ctx, "theirs").metadata
    assert audit.events[0]["payload"]["stamped_artifact_count"] == 1


def test_detach_handles_artifact_registry_without_update_metadata(ctx, registry):
    """In-memory test fixtures sometimes don't implement
 update_metadata. Service must not raise — it returns a count of
 0 stamped artifacts. The document-level state flip still wins."""

    class _MinimalRegistry:
        def list_artifacts(self, ctx, *, kind=None):
            return []
        def add(self, record): pass
        def get(self, ctx, artifact_id):
            raise ArtifactNotFoundError(artifact_id)
        def find_by_content_hash(self, ctx, h): return None

    registry.add(_document(ctx=ctx, document_id="doc-1"))
    svc = DocumentLifecycleService(
        registry=registry,
        artifact_registry=_MinimalRegistry(),  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    updated = svc.detach(ctx, "doc-1", actor="alice")
    assert updated.knowledge_state == "detached"


def test_audit_failure_does_not_break_action(ctx, registry):
    """A flaky audit sink must not block the user-facing action.
 Service catches + logs the exception and returns the updated
 record so the FE state stays consistent with disk state."""

    class _BrokenAudit:
        def record(self, *args, **kwargs):
            raise RuntimeError("audit sink unreachable")

    registry.add(_document(ctx=ctx, document_id="doc-1"))
    svc = DocumentLifecycleService(
        registry=registry,
        artifact_registry=_InMemoryArtifactRegistry(),
        audit=_BrokenAudit(),  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    updated = svc.detach(ctx, "doc-1", actor="alice")
    assert updated.knowledge_state == "detached"


# ---- Full retrieval flip --------------------------------------


def test_attach_detach_attach_round_trip_via_filter(service, ctx):
    """End-to-end: detach hides artifacts from retrieval, attach
 brings them back. Locks the user-facing contract — what the FE
 will eventually render via the Phase 6 projector."""
    svc, registry, artifacts, _ = service
    registry.add(_document(ctx=ctx, document_id="doc-1"))
    artifacts.add(_artifact(ctx=ctx, artifact_id="a-1", document_id="doc-1"))

    # Initially attached → visible.
    assert filter_to_attached_artifacts(
        artifacts.list_artifacts(ctx)
    ) == [artifacts.get(ctx, "a-1")]

    svc.detach(ctx, "doc-1", actor="alice")
    assert filter_to_attached_artifacts(
        artifacts.list_artifacts(ctx)
    ) == []

    svc.attach(ctx, "doc-1", actor="alice")
    assert len(filter_to_attached_artifacts(
        artifacts.list_artifacts(ctx)
    )) == 1
