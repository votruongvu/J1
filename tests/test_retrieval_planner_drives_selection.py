"""Follow-up patch tests: planner OWNS selection for structured intents.

Spec-required tests A-F:

  A. Structured-intent: planner selects final evidence (not
     rerank/select_by_coverage).
  B. Boilerplate in rerank's top-N is dropped when non-bp
     structured evidence exists.
  C. Boilerplate selected only as last resort + reason logged.
  D. Enriched artifact selection triggers source grounding with
     ``grounding_method`` logged.
  E. Section diversity check is intent-aware:
       * list_extraction: one section passes
       * structured mapping: hard-fails below 2 sections
  F. Existing generic_lookup behavior unchanged (backward compat).

All fixtures are generic — abstract section labels, no
domain-specific signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from j1.projects.context import ProjectContext
from j1.retrieval import (
    EVENT_CANDIDATES_RERANKED,
    EVENT_EVIDENCE_PACK_DROPPED,
    EVENT_EVIDENCE_PACK_FINALIZED,
    EVENT_EVIDENCE_PACK_SELECTED,
    QueryIntentLabel,
    RetrievalDiagnostics,
)
from j1.validation.dtos import RetrievedChunkRefDTO
from j1.validation.evidence import build_evidence_blocks


# ---- Fixture helpers ---------------------------------------------


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
    tmp_path,
    *,
    artifact_id,
    body,
    source_document_id,
    run_id,
    section_path,
    score,
    kind="compiled.text",
    chunk_id=None,
    extra_metadata: dict | None = None,
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
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        source_document_ids=[source_document_id],
        metadata=metadata,
    )
    dto = RetrievedChunkRefDTO(
        artifact_id=artifact_id,
        chunk_id=chunk_id,
        run_id=run_id,
        document_id=source_document_id,
        source_location=section_path,
        score=score,
        preview=body[:80],
        artifact_kind=kind,
    )
    return dto, rec


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t", project_id="p")


def _run(*, ctx, dtos, records, resolver, query, intent_query=None,
         active_doc="doc-A", active_run="run-A", max_blocks=5,
         diagnostics=None):
    return build_evidence_blocks(
        ctx=ctx,
        retrieved=dtos,
        artifact_registry=_StubReg(records),
        path_resolver=resolver,
        query=query,
        max_blocks=max_blocks,
        active_document_id=active_doc,
        active_run_id=active_run,
        diagnostics=diagnostics,
    )


# =====================================================================
# A. Structured-intent: planner OWNS selection
# =====================================================================
#
# Setup: a corpus where rerank's coverage-selection would pick a
# different ordering than the planner. We assert the SELECTED event
# carries ``reason_selected`` starting with ``planner_top_for_intent``,
# proving the planner produced the output.


def test_A_planner_owns_selection_for_structured_intent(ctx, tmp_path):
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-roles", body=(
                "The owner is responsible for delivering the report. "
                "The reviewer approves."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section A / Roles", score=0.9,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-deps", body=(
                "Activity 2 depends on Activity 1 output."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section B / Dependencies", score=0.8,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-stages", body=(
                "The stages of the work go from 1 through 5."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section C / Stages", score=0.7,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Who is responsible for the report?",
    )
    blocks = _run(
        ctx=ctx, dtos=[p[0] for p in pairs],
        records={p[1].artifact_id: p[1] for p in pairs},
        resolver=lambda r: tmp_path / r.location,
        query="Who is responsible for the report?",
        diagnostics=diag, max_blocks=3,
    )
    # The selected event reason must come from the planner path.
    sel_events = [
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_SELECTED
    ]
    assert sel_events, "no selected events emitted"
    for e in sel_events:
        reason = e["payload"]["reason_selected"]
        assert reason.startswith("planner_top_for_intent"), reason


# =====================================================================
# B. Boilerplate in rerank's top-N is dropped when non-bp exists
# =====================================================================


def test_B_boilerplate_dropped_when_non_boilerplate_exists(ctx, tmp_path):
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-bp", body=(
                "Insurance requirements: contractor shall maintain "
                "general liability insurance."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Exhibit B / Insurance Requirements",
            score=0.99,  # highest raw score — would top the rerank
        ),
        _make_chunk(
            tmp_path, artifact_id="A-roles", body=(
                "Owner is responsible for the report."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section A / Roles", score=0.7,
        ),
        _make_chunk(
            tmp_path, artifact_id="A-deps", body=(
                "Activity 2 depends on Activity 1."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section B / Deps", score=0.6,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Who is responsible for the report?",
    )
    blocks = _run(
        ctx=ctx, dtos=[p[0] for p in pairs],
        records={p[1].artifact_id: p[1] for p in pairs},
        resolver=lambda r: tmp_path / r.location,
        query="Who is responsible for the report?",
        diagnostics=diag, max_blocks=2,
    )
    selected_ids = {b.artifact_id for b in blocks}
    assert "A-bp" not in selected_ids, (
        f"boilerplate slipped into pack: {selected_ids}"
    )


# =====================================================================
# C. Boilerplate selected as last resort + reason logged
# =====================================================================


def test_C_boilerplate_last_resort_logged(ctx, tmp_path):
    """Corpus contains ONLY a boilerplate chunk in scope. The
    planner falls back to selecting it and logs the documented
    last-resort reason on the selected event."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-only-bp", body=(
                "Insurance requirements: contractor shall maintain "
                "general liability."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Exhibit B / Insurance Requirements",
            score=0.95,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="What are the risks in this project?",
    )
    _run(
        ctx=ctx, dtos=[p[0] for p in pairs],
        records={p[1].artifact_id: p[1] for p in pairs},
        resolver=lambda r: tmp_path / r.location,
        query="What are the risks in this project?",
        diagnostics=diag, max_blocks=2,
    )
    sel_events = [
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_SELECTED
    ]
    assert any(
        e["payload"]["reason_selected"].startswith(
            "selected_as_last_resort_no_non_boilerplate_candidate"
        )
        for e in sel_events
    )


# =====================================================================
# D. Enriched anchor triggers source grounding with method log
# =====================================================================


def test_D_source_grounding_method_logged(ctx, tmp_path):
    """An ``enriched.risks`` anchor with explicit
    ``source_chunk_ids`` metadata must trigger the planner's
    source-grounding swap; the audit must record the method."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-risks-summary",
            body=(
                "Risk register: project carries three major risks "
                "across multiple sections."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Risk Register",
            score=0.95,
            kind="enriched.risks",
            extra_metadata={
                "source_chunk_ids": ["chunk-target"],
            },
        ),
        _make_chunk(
            tmp_path, artifact_id="A-target",
            body=(
                "The principal technical risk is schedule overrun "
                "caused by external dependencies."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section B / Risks",
            score=0.40, chunk_id="chunk-target",
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="What are the major risks?",
    )
    _run(
        ctx=ctx, dtos=[p[0] for p in pairs],
        records={p[1].artifact_id: p[1] for p in pairs},
        resolver=lambda r: tmp_path / r.location,
        query="What are the major risks?",
        diagnostics=diag, max_blocks=1,
    )
    # One drop event from the planner's source-grounding swap.
    drop_events = [
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_DROPPED
        and "swapped_for_source_grounding" in str(
            e["payload"].get("reason_dropped") or ""
        )
    ]
    assert drop_events, "no source-grounding swap recorded"
    # Method appears after the colon.
    reasons = [e["payload"]["reason_dropped"] for e in drop_events]
    assert any(
        "explicit_source_chunk" in r for r in reasons
    ), (
        f"expected explicit_source_chunk grounding; "
        f"got {reasons}"
    )


# =====================================================================
# E. Section-diversity check is intent-aware
# =====================================================================


def test_E_list_extraction_passes_single_section(ctx, tmp_path):
    """One exact list section in the pack is acceptable for
    ``list_extraction`` — the section-diversity check should NOT
    hard-fail."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-list", body=(
                "Deliverables: a report, a plan, a summary."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section D / Outputs", score=0.95,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="List all the deliverables in this document.",
    )
    _run(
        ctx=ctx, dtos=[p[0] for p in pairs],
        records={p[1].artifact_id: p[1] for p in pairs},
        resolver=lambda r: tmp_path / r.location,
        query="List all the deliverables in this document.",
        diagnostics=diag, max_blocks=3,
    )
    fin = next(
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_FINALIZED
    )
    failures = fin["payload"]["check_failures"]
    assert "section_diversity_for_structured_intents" not in failures, (
        f"list_extraction should soft-warn, not hard-fail; failures={failures}"
    )


def test_E_dependency_mapping_hard_fails_below_2_sections(ctx, tmp_path):
    """Mapping intents still hard-fail when distinct sections < 2."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-deps", body=(
                "Activity 2 depends on Activity 1."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section B / Deps", score=0.95,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="Which activities depend on each other?",
    )
    _run(
        ctx=ctx, dtos=[p[0] for p in pairs],
        records={p[1].artifact_id: p[1] for p in pairs},
        resolver=lambda r: tmp_path / r.location,
        query="Which activities depend on each other?",
        diagnostics=diag, max_blocks=3,
    )
    fin = next(
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_FINALIZED
    )
    failures = fin["payload"]["check_failures"]
    assert "section_diversity_for_structured_intents" in failures


# =====================================================================
# F. Backward-compat: generic_lookup keeps legacy behaviour
# =====================================================================


def test_F_generic_lookup_uses_legacy_path(ctx, tmp_path):
    """A query that doesn't classify as a structured intent must
    keep the legacy rerank+select_by_coverage flow — selected
    events should NOT carry the planner reason."""
    pairs = [
        _make_chunk(
            tmp_path, artifact_id="A-1", body=(
                "Random unstructured text body without clear "
                "verbs the intent router keys on."
            ),
            source_document_id="doc-A", run_id="run-A",
            section_path="Section A", score=0.9,
        ),
    ]
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-A", document_id="doc-A",
        query="qwerty asdf zxcv",  # nonsense — generic_lookup
    )
    _run(
        ctx=ctx, dtos=[p[0] for p in pairs],
        records={p[1].artifact_id: p[1] for p in pairs},
        resolver=lambda r: tmp_path / r.location,
        query="qwerty asdf zxcv",
        diagnostics=diag, max_blocks=3,
    )
    # Intent.selected event records generic_lookup.
    intent_events = [
        e for e in audit.events
        if e["action"] == "j1.retrieval.intent.selected"
    ]
    assert intent_events[0]["payload"]["intent"] == "generic_lookup"
    # Selected events carry the LEGACY reason ``rerank_top`` —
    # NOT the planner reason. Backward-compat preserved.
    sel_events = [
        e for e in audit.events
        if e["action"] == EVENT_EVIDENCE_PACK_SELECTED
    ]
    for e in sel_events:
        reason = e["payload"]["reason_selected"]
        assert not reason.startswith("planner_top_for_intent"), (
            f"generic_lookup should not use planner: reason={reason}"
        )
