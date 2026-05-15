"""PR-02 contract — Query, alias, and enrichment scope safety.

Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-02, J1
MUST guarantee six scope-safety behaviours:

  1. Document A's query does not read Document B's chunks.
  2. Document A's active-snapshot query does not read its own old
     (superseded) snapshot's chunks.
  3. Project-wide query reads only documents' active snapshots.
  4. Aliases stamped under snapshot_A do not apply to snapshot_B.
  5. Enrichment artifacts attached to Document B do not load for
     Document A even when both share a snapshot id.
  6. A failed / unpromoted candidate snapshot is not default-
     queryable.

Each contract has scattered coverage today (eligibility resolver
tests, lineage hardening tests, enrichment-alias integration
tests). This module is the single PR-02 regression document: a
future refactor that breaks any of the six surfaces fails here
first with a clearly-named test.

Tests are intentionally end-to-end against the production resolver
/ loader / artifact filter — no stubs of the load-bearing modules.
Where a contract is enforced by a single function call we exercise
that function; where it's enforced by composition we drive the
composition.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.lifecycle import filter_to_attached_artifacts
from j1.documents.models import DocumentRecord
from j1.documents.snapshot import DocumentSnapshot, SnapshotState
from j1.documents.artifact_state import (
    SEARCH_STATE_SUPERSEDED,
    supersede_previous_active_artifacts,
)
from j1.errors.exceptions import DocumentNotFoundError
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.enrichment_aliases import (
    ALIAS_ARTIFACT_KIND,
    build_alias_payload,
    extract_aliases_from_text,
    load_enrichment_aliases_for_snapshot,
)
from j1.projects.context import ProjectContext
from j1.query.eligibility import resolve_eligible_active_run_ids
from j1.query.scope import ActiveScope, WorkspaceScope


_NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
_CTX = ProjectContext(tenant_id="acme", project_id="alpha")


# ---- Lightweight in-test scaffolding ----------------------------


class _StubRegistry:
    """In-memory ``SourceRegistry`` lookalike. Only the surface the
    eligibility resolver consumes is implemented; that's all we
    need to pin the contract without booting the full intake stack."""

    def __init__(self, docs: list[DocumentRecord]):
        self._by_id = {d.document_id: d for d in docs}

    def get(self, ctx, document_id):
        if document_id not in self._by_id:
            raise DocumentNotFoundError(document_id)
        return self._by_id[document_id]

    def list_documents(self, ctx):
        return list(self._by_id.values())


class _InMemoryArtifactRegistry:
    """Subset of ``ArtifactRegistry`` the alias loader and
    artifact-state filter call into. Pure in-memory so the contract
    surface is exercised without a workspace mount."""

    def __init__(self):
        self._records: list[ArtifactRecord] = []

    def add(self, record: ArtifactRecord) -> None:
        self._records.append(record)

    def list_artifacts(self, ctx, *, kind: str | None = None):
        out = []
        for r in self._records:
            if r.project.tenant_id != ctx.tenant_id:
                continue
            if r.project.project_id != ctx.project_id:
                continue
            if kind is not None and r.kind != kind:
                continue
            out.append(r)
        return out

    def update_metadata(self, ctx, artifact_id, metadata):
        for r in self._records:
            if r.artifact_id == artifact_id:
                r.metadata = dict(metadata)
                return
        raise KeyError(artifact_id)

    def get(self, ctx, artifact_id):
        for r in self._records:
            if r.artifact_id == artifact_id:
                return r
        raise KeyError(artifact_id)


def _doc(
    *, document_id: str, active_snapshot_id: str | None = "snap-active",
    state: str = "attached",
) -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id, project=_CTX,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf", file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED, created_at=_NOW,
        knowledge_state=state,  # type: ignore[arg-type]
        active_snapshot_id=active_snapshot_id,
    )


def _chunk_artifact(
    *, artifact_id: str, document_id: str, snapshot_id: str,
    body: str = "",
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id, project=_CTX,
        kind="chunk",
        location=f"chunks/{artifact_id}.json",
        content_hash=f"sha256:{artifact_id}",
        byte_size=max(1, len(body) or 1),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=[document_id],
        metadata={"snapshot_id": snapshot_id, "body": body},
        snapshot_id=snapshot_id,
        created_by_run_id=f"run-{snapshot_id}",
    )


def _alias_artifact(
    *, artifact_id: str, document_id: str, snapshot_id: str,
    text: str,
) -> ArtifactRecord:
    extracted = extract_aliases_from_text(
        text,
        run_id=f"run-{snapshot_id}",
        snapshot_id=snapshot_id,
        document_id=document_id,
    )
    payload = build_alias_payload(extracted)
    return ArtifactRecord(
        artifact_id=artifact_id, project=_CTX,
        kind=ALIAS_ARTIFACT_KIND,
        location=f"enrichment/aliases/{artifact_id}.json",
        content_hash=f"sha256:{artifact_id}", byte_size=len(text) or 1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=[document_id],
        metadata={
            "snapshot_id": snapshot_id, "payload": payload,
            "run_id": f"run-{snapshot_id}",
        },
        snapshot_id=snapshot_id,
        created_by_run_id=f"run-{snapshot_id}",
    )


# ---- Contract 1: doc-A query never reads doc-B chunks -----------


def test_contract_1_document_a_query_does_not_read_document_b_chunks():
    """ActiveScope query on Document A MUST resolve to A's snapshot
    pair only — B's snapshot id MUST NOT appear in the eligible set
    even though it also belongs to the project."""
    registry = _StubRegistry([
        _doc(document_id="doc-a", active_snapshot_id="snap-a"),
        _doc(document_id="doc-b", active_snapshot_id="snap-b"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=_CTX, scope=ActiveScope(document_id="doc-a"),
        registry=registry,
    )
    assert result.snapshot_pairs == frozenset({("doc-a", "snap-a")})
    assert "snap-b" not in result.snapshot_ids
    assert "doc-b" not in result.document_ids


# ---- Contract 2: active-scope skips superseded snapshots --------


def test_contract_2_active_scope_skips_documents_own_old_snapshot():
    """When a re-index promoted snap-new, the old snap-old's chunk
    artifacts are stamped ``search_state=superseded`` so the
    attached-artifact filter drops them. The eligibility resolver
    + that filter together guarantee Document A's active query
    never reads its own prior snapshot's chunks.

    This contract spans two production seams: the resolver
    (returns the active snapshot id) and the artifact-state filter
    (drops superseded records). Both must hold."""
    registry = _StubRegistry([
        _doc(document_id="doc-a", active_snapshot_id="snap-new"),
    ])
    # Resolver returns only the active snapshot.
    eligibility = resolve_eligible_active_run_ids(
        ctx=_CTX, scope=ActiveScope(document_id="doc-a"),
        registry=registry,
    )
    assert eligibility.snapshot_ids == frozenset({"snap-new"})

    # Artifact-state filter drops superseded records.
    artifacts = _InMemoryArtifactRegistry()
    artifacts.add(_chunk_artifact(
        artifact_id="a-old", document_id="doc-a",
        snapshot_id="snap-old",
    ))
    artifacts.add(_chunk_artifact(
        artifact_id="a-new", document_id="doc-a",
        snapshot_id="snap-new",
    ))
    supersede_previous_active_artifacts(
        ctx=_CTX, artifacts=artifacts,
        document_id="doc-a",
        new_snapshot_id="snap-new",
        previous_snapshot_id="snap-old",
    )
    visible = filter_to_attached_artifacts(
        artifacts.list_artifacts(_CTX),
    )
    visible_ids = {a.artifact_id for a in visible}
    assert visible_ids == {"a-new"}, (
        f"superseded snapshot artifacts leaked into the active "
        f"view; visible={visible_ids!r}"
    )
    # And the supersede stamp is the documented one.
    old = artifacts.get(_CTX, "a-old")
    assert old.metadata["search_state"] == SEARCH_STATE_SUPERSEDED


# ---- Contract 3: project query reads only active snapshots ------


def test_contract_3_project_query_uses_only_active_snapshots():
    """WorkspaceScope (project-wide) union must include exactly one
    snapshot per eligible document — the ``active_snapshot_id``.
    Documents without an active snapshot, detached docs, or docs
    in non-stable lifecycle states must be excluded entirely."""
    registry = _StubRegistry([
        _doc(document_id="doc-a", active_snapshot_id="snap-a"),
        _doc(document_id="doc-b", active_snapshot_id="snap-b"),
        # Detached document: has a snapshot id but knowledge_state
        # excludes it from query.
        _doc(document_id="doc-detached", active_snapshot_id="snap-x",
             state="detached"),
        # No active snapshot: never reached a successful run.
        _doc(document_id="doc-empty", active_snapshot_id=None),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=_CTX, scope=WorkspaceScope(), registry=registry,
    )
    assert result.snapshot_ids == frozenset({"snap-a", "snap-b"})
    assert result.snapshot_pairs == frozenset({
        ("doc-a", "snap-a"), ("doc-b", "snap-b"),
    })
    # Negative: no leaks of detached or empty docs.
    for forbidden in ("snap-x", None):
        assert forbidden not in result.snapshot_ids
    for forbidden in ("doc-detached", "doc-empty"):
        assert forbidden not in result.document_ids


# ---- Contract 4: aliases scoped per snapshot --------------------


def test_contract_4_aliases_do_not_leak_across_snapshots():
    """An alias artifact stamped under snap-A MUST be invisible to a
    query scoped to snap-B for the SAME document. The loader's
    snapshot filter is the contract surface."""
    artifacts = _InMemoryArtifactRegistry()
    artifacts.add(_alias_artifact(
        artifact_id="al-snap-a",
        document_id="doc-a", snapshot_id="snap-a",
        text="The bill of quantities (BOQ) must be approved.",
    ))
    # Query the SAME document at snap-B → no aliases must surface.
    out = load_enrichment_aliases_for_snapshot(
        ctx=_CTX, artifact_registry=artifacts,
        document_id="doc-a", snapshot_id="snap-b",
    )
    assert out == (), (
        "alias loader leaked an artifact from snap-A into a snap-B "
        f"query; got {out!r}"
    )
    # Sanity: same loader at snap-A DOES surface the alias — proves
    # the filter is keying on snapshot, not on the absence of any
    # artifact at all.
    same_scope = load_enrichment_aliases_for_snapshot(
        ctx=_CTX, artifact_registry=artifacts,
        document_id="doc-a", snapshot_id="snap-a",
    )
    assert len(same_scope) >= 1
    assert any(b.canonical_name == "bill of quantities" for b in same_scope)


# ---- Contract 5: enrichment artifacts scoped per document -------


def test_contract_5_enrichment_artifacts_do_not_load_across_documents():
    """An alias artifact attached to Document B MUST NOT load for
    Document A even when both share a snapshot id (unusual but
    possible in tests / migrations). The loader's
    ``source_document_ids`` filter is the contract surface."""
    artifacts = _InMemoryArtifactRegistry()
    # The alias is stamped under doc-other but ALSO under
    # snap-shared. Document-id filter must reject it for doc-target.
    artifacts.add(_alias_artifact(
        artifact_id="al-cross-doc",
        document_id="doc-other", snapshot_id="snap-shared",
        text="Reference the bill of quantities (BOQ) before each cycle.",
    ))
    out = load_enrichment_aliases_for_snapshot(
        ctx=_CTX, artifact_registry=artifacts,
        document_id="doc-target", snapshot_id="snap-shared",
    )
    assert out == (), (
        "alias loader leaked a doc-other artifact into a doc-target "
        f"query (same snapshot); got {out!r}"
    )


# ---- Contract 6: unpromoted snapshot is not default-queryable ---


def test_contract_6_failed_unpromoted_snapshot_not_default_queryable():
    """A document whose latest run failed has NO ``active_snapshot_id``.
    The eligibility resolver MUST refuse the document for both
    ActiveScope and WorkspaceScope queries — surfacing a failed
    candidate by default would silently expose half-built knowledge."""
    registry = _StubRegistry([
        _doc(document_id="doc-failing", active_snapshot_id=None),
    ])
    active_result = resolve_eligible_active_run_ids(
        ctx=_CTX, scope=ActiveScope(document_id="doc-failing"),
        registry=registry,
    )
    assert active_result.snapshot_ids == frozenset()
    assert active_result.is_empty

    project_result = resolve_eligible_active_run_ids(
        ctx=_CTX, scope=WorkspaceScope(), registry=registry,
    )
    assert project_result.snapshot_ids == frozenset()
    assert "doc-failing" not in project_result.document_ids


# ---- Bonus: cross-tenant isolation -----------------------------


def test_alias_loader_does_not_cross_tenant_boundary():
    """Bonus contract — the artifact registry is `(ctx, ...)`-keyed.
    The same alias loader called with a different tenant/project
    ctx returns nothing even when an artifact with the same
    document_id + snapshot_id exists in another tenant's data.

    Pinned so a future refactor that drops the ctx parameter on
    the loader / registry list cannot silently leak across
    tenants."""
    artifacts = _InMemoryArtifactRegistry()
    artifacts.add(_alias_artifact(
        artifact_id="al-acme",
        document_id="doc-target", snapshot_id="snap-active",
        text="Reference the bill of quantities (BOQ) before each cycle.",
    ))
    foreign_ctx = ProjectContext(
        tenant_id="other-tenant", project_id="other-project",
    )
    out = load_enrichment_aliases_for_snapshot(
        ctx=foreign_ctx, artifact_registry=artifacts,
        document_id="doc-target", snapshot_id="snap-active",
    )
    assert out == ()
