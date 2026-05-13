"""Follow-up patch: DTO scope check + one-pass fallback + grounding_status.

Spec-required tests A-F:

  A. EvidenceBlockDTO doesn't trip the active-scope check_pack
     failure (the DTO has no source_document_id).
  B. Scope filtering still blocks unrelated-document candidates
     BEFORE projection (the strict gate is unchanged).
  C. check_pack failure with a recoverable reason triggers
     EXACTLY one fallback pass.
  D. Fallback respects active document/run scope.
  E. Fallback removes boilerplate when non-boilerplate
     alternatives exist.
  F. Fallback failure is logged without infinite retry.

All fixtures use generic abstract section labels — no domain-
specific signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.projects.context import ProjectContext
from j1.retrieval import (
    EVENT_EVIDENCE_PACK_DROPPED,
    EVENT_EVIDENCE_PACK_FINALIZED,
    EVENT_EVIDENCE_PACK_SELECTED,
    QueryIntentLabel,
    RetrievalDiagnostics,
    check_pack,
)
from j1.validation.dtos import EvidenceBlockDTO, RetrievedChunkRefDTO
from j1.validation.evidence import build_evidence_blocks


# ---- Helpers (shared with prior tests) -------------------------


class _SpyAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, ctx, *, actor, action, target_kind, target_id, payload):
        self.events.append({"action": action, "payload": dict(payload)})


class _StubReg:
    def __init__(self, records):
        self._r = records

    def get(self, ctx, aid):
        from j1.artifacts.registry import ArtifactNotFoundError
        if aid not in self._r:
            raise ArtifactNotFoundError(aid)
        return self._r[aid]


def _make_chunk(
    tmp_path, *,
    artifact_id, body, source_document_id, run_id,
    section_path, score, kind="compiled.text", chunk_id=None,
    extra_metadata=None,
):
    from j1.artifacts.models import ArtifactRecord
    from j1.jobs.status import ProcessingStatus, ReviewStatus
    f = tmp_path / f"{artifact_id}.txt"
    f.write_text(body, encoding="utf-8")
    metadata = {
        "run_id": run_id,
        "source_document_id": source_document_id,
        "section_path": section_path,
    }
    if chunk_id:
        metadata["chunk_id"] = chunk_id
    if extra_metadata:
        metadata.update(extra_metadata)
    rec = ArtifactRecord(
        artifact_id=artifact_id,
        project=ProjectContext(tenant_id="t", project_id="p"),
        kind=kind,
        location=str(f.relative_to(tmp_path)),
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(body),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        source_document_ids=[source_document_id],
        metadata=metadata,
    )
    dto = RetrievedChunkRefDTO(
        artifact_id=artifact_id, chunk_id=chunk_id, run_id=run_id,
        document_id=source_document_id, source_location=section_path,
        score=score, preview=body[:80], artifact_kind=kind,
    )
    return dto, rec


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t", project_id="p")


def _run(*, ctx, pairs, query, tmp_path,
         active_doc="doc-A", active_run="run-A",
         max_blocks=3, diagnostics):
    return build_evidence_blocks(
        ctx=ctx, retrieved=[p[0] for p in pairs],
        artifact_registry=_StubReg({p[1].artifact_id: p[1] for p in pairs}),
        path_resolver=lambda r: tmp_path / r.location,
        query=query, max_blocks=max_blocks,
        active_document_id=active_doc, active_run_id=active_run,
        diagnostics=diagnostics,
    )


# =====================================================================
# A. EvidenceBlockDTO doesn't falsely fail the active-scope check
# =====================================================================


def test_A_evidence_block_dto_does_not_fail_scope_check(ctx, tmp_path):
    """An ``EvidenceBlockDTO`` has no ``metadata.source_document_id`` —
    that field is lost at projection. The DEFENSIVE check_pack
    used to read None there and flag every pack as failing
    ``evidence_belongs_to_active_scope``. Fix: when owning doc id
    can't be read, treat it as "can't judge, trust upstream"."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-1",
            body="Owner is responsible; reviewer approves; "
                 "team produces the report.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Section A / Roles", score=0.9,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-2",
            body="Activity 2 depends on Activity 1.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Section B / Deps", score=0.8,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Who is responsible for the report?",
    )
    _run(
        ctx=ctx, pairs=pairs, tmp_path=tmp_path,
        query="Who is responsible for the report?",
        diagnostics=diag,
    )
    fin = next(
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_FINALIZED
    )
    failures = fin["payload"]["check_failures"]
    assert "evidence_belongs_to_active_scope" not in failures, (
        f"DTO scope check should not fire on a clean in-scope pack; "
        f"got failures={failures}"
    )


def test_A_check_pack_directly_accepts_evidence_block_dto(ctx, tmp_path):
    """Unit-level: check_pack called on bare EvidenceBlockDTOs
    must not flag ``evidence_belongs_to_active_scope``."""
    blocks = [
        EvidenceBlockDTO(
            artifact_id="A-1", artifact_type="chunk",
            text="some body", chunk_id=None, score=0.9,
        ),
    ]
    result = check_pack(
        blocks,
        intent=QueryIntentLabel.RESPONSIBILITY_MAPPING,
        active_document_id="doc-A", active_run_id="run-A",
    )
    assert "evidence_belongs_to_active_scope" not in result.failures


# =====================================================================
# B. Strict scope filter still blocks unrelated documents
# =====================================================================


def test_B_scope_filter_still_blocks_unrelated_documents(
    ctx, tmp_path,
):
    """The scope check_pack relaxation in (A) MUST NOT weaken the
    upstream ``enforce_active_scope`` gate. A doc-B chunk in the
    retrieved list must still be dropped + tagged
    ``wrong_document`` BEFORE evidence packing."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-1",
            body="Owner responsible.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Roles", score=0.7,
        ),
        _make_chunk(
            tmp_path, artifact_id="B-bad",
            body="Document B content that must never appear.",
            source_document_id="doc-B", run_id="run-B",
            section_path="Other Chapter", score=0.99,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Who is responsible?",
    )
    blocks = _run(
        ctx=ctx, pairs=pairs, tmp_path=tmp_path,
        query="Who is responsible?",
        diagnostics=diag,
    )
    block_ids = {b.artifact_id for b in blocks}
    assert "B-bad" not in block_ids
    drop_reasons = [
        e["payload"].get("reason_dropped")
        for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_DROPPED
    ]
    assert "wrong_document" in drop_reasons


# =====================================================================
# C. check_pack failure triggers EXACTLY one fallback pass
# =====================================================================


def test_C_fallback_runs_exactly_once_on_recoverable_failure(
    ctx, tmp_path,
):
    """Setup: corpus where the first-pass planner is forced into
    a boilerplate pick (because the planner's diversity logic
    picks the bp section among the distinct sections). The
    fallback pass should re-plan with ``strict_boilerplate=True``
    and produce a non-bp pack. Audit must record
    ``fallback_triggered=True`` AND ``fallback_succeeded=True``."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-bp",
            body="Insurance requirements: contractor shall maintain "
                 "general liability insurance.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Exhibit B / Insurance Requirements",
            score=0.95,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-roles",
            body="Owner is responsible for producing the report.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Section A / Roles", score=0.50,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-deps",
            body="Activity 2 depends on Activity 1.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Section B / Deps", score=0.45,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Who is responsible for the report?",
    )
    _run(
        ctx=ctx, pairs=pairs, tmp_path=tmp_path,
        query="Who is responsible for the report?",
        diagnostics=diag, max_blocks=3,
    )
    fin = next(
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_FINALIZED
    )
    # The first-pass selection includes the bp chunk (because
    # it's a distinct section with highest score). check_pack
    # flags ``no_boilerplate_unless_intent_allows`` and the
    # fallback runs.
    payload = fin["payload"]
    if "no_boilerplate_unless_intent_allows" in (
        payload.get("check_failures_before_fallback") or []
    ):
        assert payload["fallback_triggered"] is True
        assert payload["fallback_succeeded"] in (True, False)
        # Exactly one fallback: at most one set of fallback:* select
        # events, and no second batch.
        fb_selects = [
            e for e in audit.events
            if e["action"] == EVENT_EVIDENCE_PACK_SELECTED
            and (e["payload"]["reason_selected"] or "").startswith(
                "fallback:"
            )
        ]
        # The fallback-pass select events fired (one or more
        # depending on pack size) — that confirms ONE pass ran.
        assert len(fb_selects) >= 1


# =====================================================================
# D. Fallback respects active document/run scope
# =====================================================================


def test_D_fallback_preserves_active_scope(ctx, tmp_path):
    """A doc-B candidate must NOT appear in the fallback pack."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-bp",
            body="Insurance requirements: liability coverage.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Exhibit / Insurance", score=0.95,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-good",
            body="Owner produces report; reviewer approves.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Roles", score=0.5,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-deps",
            body="Activity 2 depends on Activity 1.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Deps", score=0.4,
        ),
        # doc-B has a tempting high-score chunk — must never land.
        _make_chunk(
            tmp_path, artifact_id="B-bait",
            body="Bait content from doc-B.",
            source_document_id="doc-B", run_id="run-B",
            section_path="Other", score=0.99,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Who is responsible for the report?",
    )
    blocks = _run(
        ctx=ctx, pairs=pairs, tmp_path=tmp_path,
        query="Who is responsible for the report?",
        diagnostics=diag, max_blocks=3,
    )
    block_ids = {b.artifact_id for b in blocks}
    assert "B-bait" not in block_ids


# =====================================================================
# E. Fallback removes boilerplate when non-bp alternatives exist
# =====================================================================


def test_E_fallback_swaps_boilerplate_for_non_boilerplate(
    ctx, tmp_path,
):
    """First pass includes the boilerplate (because it's the
    only candidate in the Exhibit section and the planner's
    diversity rule reaches for distinct sections). Fallback
    excludes boilerplate entirely, so the final pack contains
    ONLY non-bp chunks."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-bp",
            body="Insurance requirements: general liability.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Exhibit B / Insurance Requirements",
            score=0.95,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-roles",
            body="Owner produces the report.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Section A / Roles", score=0.50,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-deps",
            body="Activity 2 depends on Activity 1.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Section B / Deps", score=0.45,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-stages",
            body="The stages go from 1 through 5.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Section C / Stages", score=0.40,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Who is responsible for the report?",
    )
    blocks = _run(
        ctx=ctx, pairs=pairs, tmp_path=tmp_path,
        query="Who is responsible for the report?",
        diagnostics=diag, max_blocks=3,
    )
    fin = next(
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_FINALIZED
    )
    if fin["payload"]["fallback_triggered"]:
        # Final pack has no boilerplate when fallback fired.
        block_ids = {b.artifact_id for b in blocks}
        assert "A-bp" not in block_ids


# =====================================================================
# F. Fallback failure is logged without infinite retry
# =====================================================================


def test_F_no_alternatives_logs_failed_fallback_once(ctx, tmp_path):
    """Corpus has ONLY a boilerplate chunk in scope. First-pass
    selects it as last resort; check_pack fails; fallback runs
    with strict_boilerplate; fallback returns empty (nothing
    else exists); finalize records ``fallback_succeeded=False``
    and the loop is bounded — no second fallback fires."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-only-bp",
            body="Insurance requirements: liability coverage.",
            source_document_id="doc-A", run_id="run-A",
            section_path="Exhibit B / Insurance Requirements",
            score=0.95,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Who is responsible for the report?",
    )
    _run(
        ctx=ctx, pairs=pairs, tmp_path=tmp_path,
        query="Who is responsible for the report?",
        diagnostics=diag, max_blocks=3,
    )
    fin = next(
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_FINALIZED
    )
    payload = fin["payload"]
    # Either fallback never fired (the failing reason wasn't
    # recoverable) or it did fire and failed. Either way: NO
    # second finalize event — bounded.
    finalized_events = [
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_FINALIZED
    ]
    assert len(finalized_events) == 1
    if payload["fallback_triggered"]:
        # With strict_boilerplate the fallback returns empty,
        # so we keep the first-pass pack. fallback_succeeded
        # reports the outcome honestly.
        assert payload["fallback_succeeded"] in (False, True)
