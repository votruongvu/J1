"""Generic regression tests for the retrieval-quality patch.

NO domain-specific signals in this file. Every fixture uses
abstract section labels like ``Section A``, ``Section B``,
generic verbs in queries, and a two-document corpus so the
scope-isolation tests pin the contamination bug we're guarding
against.

Test groups (mirror the user's spec sections):

  * **scope isolation** — multi-document corpus; a query for
    one document must drop the other document's chunks with a
    structured drop reason in the diagnostic stream.
  * **intent routing** — 16 generic intents pin against
    representative queries. No customer-named keywords.
  * **boilerplate filter** — analytical queries demote boilerplate;
    legal-terms queries keep it.
  * **evidence planning** — diversity-leaning intents pick
    distinct section paths; non-diversity intents stay top-K.
  * **quality checks** — each check fires correctly + fallback
    path is triggered by the right failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from j1.retrieval import (
    CandidateDiagnostic,
    DropReason,
    EVENT_CANDIDATES_RETRIEVED,
    EVENT_EVIDENCE_PACK_DROPPED,
    EVENT_INTENT_SELECTED,
    EVENT_QUERY_RECEIVED,
    EVENT_SCOPE_APPLIED,
    QueryIntentLabel,
    RetrievalDiagnostics,
    annotate_scope,
    boilerplate_demotion,
    check_pack,
    detect_intent,
    enforce_active_scope,
    is_boilerplate_chunk,
    plan_evidence,
)
from j1.retrieval.boilerplate import BoilerplateCategory


# ---- Test helpers ------------------------------------------------


@dataclass
class _FakeCand:
    """Minimal in-test candidate. Mirrors the shape both BM25
    SearchHit and LightRAG payloads share — ``artifact_id``,
    ``artifact_type``, ``score``, ``metadata`` dict, and
    optional rerank_score."""

    artifact_id: str
    artifact_type: str = "chunk"
    score: float = 0.5
    rerank_score: float | None = None
    section_path: str | None = None
    title: str | None = None
    source_document_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    text: str = ""


class _SpyAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, ctx, *, actor, action, target_kind, target_id, payload):
        self.events.append({
            "action": action,
            "target_id": target_id,
            "payload": dict(payload),
        })


def _make_diag(ctx, *, run_id="run-1", document_id="doc-A", query="q"):
    return RetrievalDiagnostics(
        audit=_SpyAudit(),
        ctx=ctx,
        run_id=run_id,
        document_id=document_id,
        query=query,
    )


# =====================================================================
# 1. SCOPE ISOLATION — two-document corpus
# =====================================================================
#
# The core contamination guard: when the active document is A, a
# candidate belonging to document B must be dropped with a
# ``wrong_document`` reason in the audit stream.


def _two_doc_corpus() -> list[_FakeCand]:
    """Build a tiny two-document corpus using generic section
    names (no customer-specific labels)."""
    return [
        _FakeCand(
            artifact_id="a-1", section_path="Section A / Overview",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
        _FakeCand(
            artifact_id="a-2", section_path="Section A / Details",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
        _FakeCand(
            artifact_id="b-1", section_path="Chapter 1 / Intro",
            metadata={"source_document_id": "doc-B", "run_id": "run-B"},
        ),
        _FakeCand(
            artifact_id="b-2", section_path="Chapter 1 / Body",
            metadata={"source_document_id": "doc-B", "run_id": "run-B"},
        ),
        # Unscoped candidate — has no source_document_id at all.
        # Strict filter must reject under ``no_scope_metadata``.
        _FakeCand(
            artifact_id="orphan",
            section_path="Floating section",
            metadata={},
        ),
    ]


def test_scope_admits_only_active_document(ctx):
    """A query for doc-A receives ONLY doc-A candidates; doc-B
    and the orphan are dropped with the correct DropReason."""
    diag = _make_diag(ctx)
    admitted, _scopes = enforce_active_scope(
        _two_doc_corpus(),
        active_document_id="doc-A",
        active_run_id=None,
        diagnostics=diag,
    )
    assert {c.artifact_id for c in admitted} == {"a-1", "a-2"}
    # Audit log carries one drop per excluded candidate.
    dropped_ids = {c.artifact_id for c in diag.dropped}
    assert dropped_ids == {"b-1", "b-2", "orphan"}
    # Reasons are structured.
    reason_by_id = {c.artifact_id: c.reason_dropped for c in diag.dropped}
    assert reason_by_id["b-1"] == DropReason.WRONG_DOCUMENT.value
    assert reason_by_id["b-2"] == DropReason.WRONG_DOCUMENT.value
    assert reason_by_id["orphan"] == DropReason.NO_SCOPE_METADATA.value


def test_scope_admits_all_when_no_active_scope_supplied(ctx):
    """Cross-document search (no active doc) admits everything
    and emits no drops."""
    diag = _make_diag(ctx)
    admitted, _ = enforce_active_scope(
        _two_doc_corpus(),
        active_document_id=None,
        active_run_id=None,
        diagnostics=diag,
    )
    assert len(admitted) == 5
    assert len(diag.dropped) == 0


def test_scope_run_id_filter_after_doc_match(ctx):
    """Active run filter applies after the document gate. A
    chunk from the right document but the WRONG run is
    dropped with ``wrong_run``."""
    candidates = [
        _FakeCand(
            artifact_id="a-current", section_path="Section A",
            metadata={"source_document_id": "doc-A", "run_id": "run-A2"},
        ),
        _FakeCand(
            artifact_id="a-old", section_path="Section A",
            metadata={"source_document_id": "doc-A", "run_id": "run-A1"},
        ),
    ]
    diag = _make_diag(ctx)
    admitted, _ = enforce_active_scope(
        candidates,
        active_document_id="doc-A",
        active_run_id="run-A2",
        diagnostics=diag,
    )
    assert [c.artifact_id for c in admitted] == ["a-current"]
    assert diag.dropped[0].reason_dropped == DropReason.WRONG_RUN.value


def test_annotate_scope_extracts_from_alt_metadata_layouts(ctx):
    """``annotate_scope`` reads from either top-level fields, the
    metadata dict, or the source_document_ids list (when length 1).
    A multi-source list is treated as unscoped — the filter then
    rejects with ``NO_SCOPE_METADATA``."""
    # Top-level field.
    c1 = _FakeCand(
        artifact_id="x",
        metadata={"source_document_id": "doc-Z"},
    )
    s1 = annotate_scope(c1)
    assert s1.source_document_id == "doc-Z"

    # source_document_ids list with one entry.
    c2 = _FakeCand(
        artifact_id="y",
        source_document_ids=["doc-Y"],
    )
    s2 = annotate_scope(c2)
    assert s2.source_document_id == "doc-Y"

    # Ambiguous multi-source — returns None.
    c3 = _FakeCand(
        artifact_id="z",
        source_document_ids=["doc-1", "doc-2"],
    )
    s3 = annotate_scope(c3)
    assert s3.source_document_id is None
    assert not s3.has_any_scope() or s3.source_document_id is None


# =====================================================================
# 2. INTENT ROUTING — 16 generic intents
# =====================================================================


@pytest.mark.parametrize("query,expected", [
    (
        "List all the deliverables produced by the team.",
        QueryIntentLabel.LIST_EXTRACTION,
    ),
    (
        "Summarize this document.",
        QueryIntentLabel.SUMMARY_LOOKUP,
    ),
    (
        "How many sections are there?",
        QueryIntentLabel.EXACT_FACT_LOOKUP,
    ),
    (
        "What are the requirements for the system?",
        QueryIntentLabel.REQUIREMENTS_LOOKUP,
    ),
    (
        "Who is responsible for the design review?",
        QueryIntentLabel.RESPONSIBILITY_MAPPING,
    ),
    (
        "Which activities depend on the geophysical survey output?",
        QueryIntentLabel.DEPENDENCY_MAPPING,
    ),
    (
        "How do the deliverables evolve from one stage to the next?",
        QueryIntentLabel.STAGE_PROGRESSION,
    ),
    (
        "What outputs does the team produce as final deliverables?",
        QueryIntentLabel.DELIVERABLE_MAPPING,
    ),
    (
        "What are the major risks and uncertainties?",
        QueryIntentLabel.ISSUE_RISK_MAPPING,
    ),
    (
        "What was the rationale for selecting this approach?",
        QueryIntentLabel.DECISION_TRACE,
    ),
    (
        "Compare option A versus option B.",
        QueryIntentLabel.COMPARISON,
    ),
    (
        "What compliance criteria does the project conform to?",
        QueryIntentLabel.COMPLIANCE_LOOKUP,
    ),
    (
        "What are the contractual notices provisions?",
        QueryIntentLabel.LEGAL_OR_CONTRACT_TERMS,
    ),
    (
        "What is the project budget?",
        QueryIntentLabel.COST_OR_EFFORT_LOOKUP,
    ),
    (
        "What is the project timeline?",
        QueryIntentLabel.SCHEDULE_OR_MILESTONE_LOOKUP,
    ),
    (
        "asdf qwerty",
        QueryIntentLabel.GENERIC_LOOKUP,
    ),
])
def test_intent_router_classifies_generic_queries(query, expected):
    det = detect_intent(query)
    assert det.intent == expected, (
        f"query={query!r} got={det.intent.value} "
        f"top_scores={det.scores}"
    )


def test_intent_router_emits_signals_payload():
    det = detect_intent("Who is responsible for the design review?")
    payload = det.signals_payload()
    assert "winning_score" in payload
    assert payload["winning_score"] > 0
    assert "scores" in payload
    assert "responsibility_mapping" in payload["scores"]


def test_intent_router_tie_break_specificity_wins():
    """When two intents tie, the more specific shape wins per
    the priority table. ``list of risks`` should classify as
    ISSUE_RISK_MAPPING (specific) NOT LIST_EXTRACTION (generic)."""
    det = detect_intent("List all the major risks of the project.")
    assert det.intent == QueryIntentLabel.ISSUE_RISK_MAPPING


# =====================================================================
# 3. BOILERPLATE FILTER
# =====================================================================


@pytest.mark.parametrize("section_path,expected_category", [
    (
        "Exhibit B / Insurance Requirements",
        BoilerplateCategory.INSURANCE_REQUIREMENTS,
    ),
    (
        "Article 12 / Notices shall be in writing",
        BoilerplateCategory.NOTICES_PROVISION,
    ),
    (
        "Article 14 / Executed in counterparts",
        BoilerplateCategory.EXECUTION_COUNTERPARTS,
    ),
    (
        "Standard Terms / Entire Agreement / Severability",
        BoilerplateCategory.STANDARD_TERMS,
    ),
    (
        "Section 1 / Proposal Format Requirements",
        BoilerplateCategory.ADMINISTRATIVE_INSTRUCTIONS,
    ),
    (
        "In Witness Whereof, the parties have executed",
        BoilerplateCategory.SIGNATURE_BLOCK,
    ),
])
def test_boilerplate_detector_matches_generic_categories(
    section_path, expected_category,
):
    m = is_boilerplate_chunk(section_path=section_path)
    assert m is not None
    assert m.category == expected_category


def test_boilerplate_detector_returns_none_for_normal_content():
    m = is_boilerplate_chunk(
        section_path="Chapter 4 / Investigation activities",
        body_preview=(
            "The team will conduct an investigation across the "
            "site, producing a report that feeds into the design."
        ),
    )
    assert m is None


def test_boilerplate_demotion_aggressive_for_analytical_intents():
    """Risk/dependency/responsibility intents demote insurance/
    agreement chunks to ≤ 0.10 of their original score."""
    for intent in (
        QueryIntentLabel.ISSUE_RISK_MAPPING,
        QueryIntentLabel.RESPONSIBILITY_MAPPING,
        QueryIntentLabel.DEPENDENCY_MAPPING,
    ):
        mult = boilerplate_demotion(
            BoilerplateCategory.INSURANCE_REQUIREMENTS, intent,
        )
        assert mult <= 0.10, f"intent={intent.value} mult={mult}"


def test_boilerplate_demotion_skipped_for_legal_or_compliance_intents():
    """When the user is ASKING about contract terms, the demotion
    is bypassed (multiplier 1.0)."""
    mult = boilerplate_demotion(
        BoilerplateCategory.INSURANCE_REQUIREMENTS,
        QueryIntentLabel.LEGAL_OR_CONTRACT_TERMS,
    )
    assert mult == 1.0


# =====================================================================
# 4. EVIDENCE PLANNER — structure-aware selection
# =====================================================================


def _diverse_candidates() -> list[_FakeCand]:
    """Six candidates across THREE distinct generic sections.
    Scores ordered so that without diversity logic, top-3 would
    be three chunks from the same section."""
    return [
        _FakeCand(
            "high-1", rerank_score=10.0,
            section_path="Section A / Sub 1",
        ),
        _FakeCand(
            "high-2", rerank_score=9.5,
            section_path="Section A / Sub 2",
        ),
        _FakeCand(
            "high-3", rerank_score=9.0,
            section_path="Section A / Sub 3",
        ),
        _FakeCand(
            "mid-1", rerank_score=5.0,
            section_path="Section B / Sub 1",
        ),
        _FakeCand(
            "mid-2", rerank_score=4.0,
            section_path="Section C / Sub 1",
        ),
        _FakeCand(
            "low-1", rerank_score=1.0,
            section_path="Section A / Sub 4",
        ),
    ]


def test_planner_diversity_intent_picks_distinct_sections():
    """A diversity-leaning intent (responsibility) must NOT pack
    three same-section chunks even if they're top-3 by score —
    the section diversity quota wins."""
    plan = plan_evidence(
        _diverse_candidates(),
        intent=QueryIntentLabel.RESPONSIBILITY_MAPPING,
        max_blocks=5,
    )
    picked_sections = {
        c.section_path.split(" / ")[0] for c in plan.selected
    }
    # 3 distinct top-level sections present in the corpus.
    assert picked_sections == {"Section A", "Section B", "Section C"}


def test_planner_non_diversity_intent_keeps_top_k():
    """Exact-fact lookup keeps top-K by score, no diversity
    rewrite. Top-3 should be the three highest scores from
    Section A."""
    plan = plan_evidence(
        _diverse_candidates(),
        intent=QueryIntentLabel.EXACT_FACT_LOOKUP,
        max_blocks=3,
    )
    picked_ids = [c.artifact_id for c in plan.selected]
    assert picked_ids == ["high-1", "high-2", "high-3"]


def test_planner_source_grounding_swap_when_only_enriched():
    """Issue-risk intent prefers enriched.risks anchors but
    requires at least one source chunk. When the pack is all
    enriched, the planner swaps the lowest-score enriched block
    for the best source chunk."""
    candidates = [
        _FakeCand(
            "enriched-risks-1", artifact_type="enriched.risks",
            rerank_score=10.0, section_path="Risks / Overview",
        ),
        _FakeCand(
            "enriched-risks-2", artifact_type="enriched.risks",
            rerank_score=9.0, section_path="Risks / Detail",
        ),
        _FakeCand(
            "chunk-1", artifact_type="chunk",
            rerank_score=5.0, section_path="Section A",
        ),
    ]
    plan = plan_evidence(
        candidates,
        intent=QueryIntentLabel.ISSUE_RISK_MAPPING,
        max_blocks=2,
    )
    kinds = {c.artifact_type for c in plan.selected}
    assert "chunk" in kinds
    assert "enriched.risks" in kinds
    # The dropped report explains the swap WITH the grounding
    # method appended (default is heuristic_best_score when the
    # enriched anchor has no explicit source_chunk_ids etc.).
    drop_reasons = {reason for _, reason in plan.dropped}
    assert any(
        r.startswith("swapped_for_source_grounding:")
        for r in drop_reasons
    )
    assert "heuristic_best_score" in " ".join(drop_reasons)


@pytest.mark.parametrize(
    "anchor_meta,source_chunks,expected_method,expected_chunk_id",
    [
        # explicit_source_chunk wins over a higher-scoring chunk
        # whose chunk_id doesn't match the anchor's list.
        (
            {"source_chunk_ids": ["chunk-target"]},
            [
                ("chunk-noise", 9.0, None, "Section X"),
                ("chunk-target", 1.0, "chunk-target", "Section A"),
            ],
            "explicit_source_chunk", "chunk-target",
        ),
        # source_artifact_section: anchor names source_artifact_id,
        # section_path overlaps.
        (
            {
                "source_artifact_id": "art-7",
                "section_path": "Chapter 3 / Risk Register",
            },
            [
                ("chunk-x", 8.0, None, "Chapter 1"),
                # Same artifact + overlapping section wins even
                # though scored lower.
                (
                    "chunk-7", 2.0, None,
                    "Chapter 3 / Risk Register",
                ),
            ],
            "source_artifact_section", "chunk-7",
        ),
        # heuristic_best_score: no metadata signals at all.
        (
            {},
            [
                ("chunk-low", 1.0, None, "Section A"),
                ("chunk-high", 9.0, None, "Section B"),
            ],
            "heuristic_best_score", "chunk-high",
        ),
    ],
)
def test_grounding_method_picks_correct_source(
    anchor_meta, source_chunks, expected_method, expected_chunk_id,
):
    """Direct unit on the grounding picker. Each row covers one
    of the documented methods so the spec's grounding ladder is
    exercised end-to-end."""
    from j1.retrieval.evidence_planner import _pick_grounding_source

    anchor = _FakeCand(
        "anchor", artifact_type="enriched.risks",
        rerank_score=10.0, metadata=anchor_meta,
    )
    pool = []
    for art_id, score, chunk_id, section in source_chunks:
        c = _FakeCand(
            art_id, artifact_type="chunk",
            rerank_score=score, section_path=section,
            metadata={"section_path": section},
        )
        if chunk_id is not None:
            c.metadata["chunk_id"] = chunk_id
            setattr(c, "chunk_id", chunk_id)
        # source_artifact_id on the chunk's metadata, when relevant
        if expected_method == "source_artifact_section":
            c.metadata["source_artifact_id"] = (
                "art-7" if art_id == "chunk-7" else "art-other"
            )
        pool.append(c)

    chunk, method = _pick_grounding_source(
        enriched_anchor=anchor, source_pool=pool,
    )
    assert method == expected_method
    assert chunk.artifact_id == expected_chunk_id


def test_planner_avoid_kinds_penalized_but_not_removed():
    """``permit/list_extraction`` should avoid enriched.document_map
    in favour of source chunks — but when nothing else is
    available the avoid-listed kind still lands rather than the
    pack being empty."""
    only_summary = [
        _FakeCand(
            "summary",
            artifact_type="enriched.document_map",
            rerank_score=10.0,
            section_path="Document Map",
        ),
    ]
    plan = plan_evidence(
        only_summary,
        intent=QueryIntentLabel.LIST_EXTRACTION,
        max_blocks=3,
    )
    assert len(plan.selected) == 1
    assert plan.selected[0].artifact_id == "summary"


# =====================================================================
# 5. QUALITY CHECKS + fallback trigger
# =====================================================================


def test_check_pack_passes_when_well_formed():
    pack = [
        _FakeCand(
            "a", artifact_type="chunk",
            section_path="Section A",
            metadata={"source_document_id": "doc-A", "run_id": "run-1"},
        ),
        _FakeCand(
            "b", artifact_type="chunk",
            section_path="Section B",
            metadata={"source_document_id": "doc-A", "run_id": "run-1"},
        ),
    ]
    result = check_pack(
        pack, intent=QueryIntentLabel.DEPENDENCY_MAPPING,
        active_document_id="doc-A", active_run_id="run-1",
    )
    assert result.ok, result.failures


def test_check_pack_fails_on_empty():
    result = check_pack(
        [], intent=QueryIntentLabel.RESPONSIBILITY_MAPPING,
        active_document_id="doc-A", active_run_id=None,
    )
    assert not result.ok
    assert "evidence_pack_non_empty" in result.failures


def test_check_pack_fails_on_mixed_documents():
    pack = [
        _FakeCand(
            "a", section_path="Section A",
            metadata={"source_document_id": "doc-A"},
        ),
        _FakeCand(
            "b", section_path="Section A",
            metadata={"source_document_id": "doc-B"},
        ),
    ]
    result = check_pack(
        pack, intent=QueryIntentLabel.SUMMARY_LOOKUP,
        active_document_id=None, active_run_id=None,
    )
    assert not result.ok
    assert "no_unrelated_document_evidence" in result.failures


def test_check_pack_rejects_boilerplate_for_analytical_intent():
    pack = [
        _FakeCand(
            "ins", section_path="Exhibit B / Insurance Requirements",
            metadata={"source_document_id": "doc-A"},
        ),
    ]
    result = check_pack(
        pack, intent=QueryIntentLabel.ISSUE_RISK_MAPPING,
        active_document_id="doc-A", active_run_id=None,
    )
    assert not result.ok
    assert "no_boilerplate_unless_intent_allows" in result.failures


def test_check_pack_allows_boilerplate_for_legal_intent():
    pack = [
        _FakeCand(
            "ins", section_path="Exhibit B / Insurance Requirements",
            metadata={"source_document_id": "doc-A"},
        ),
    ]
    result = check_pack(
        pack, intent=QueryIntentLabel.LEGAL_OR_CONTRACT_TERMS,
        active_document_id="doc-A", active_run_id=None,
    )
    assert result.ok


def test_check_pack_fails_diversity_for_structured_intent():
    """A responsibility-mapping pack with all blocks from ONE
    section path fails the diversity check (hard fail when
    distinct_paths < 2)."""
    pack = [
        _FakeCand(
            "a1", section_path="Section A",
            metadata={"source_document_id": "doc-A"},
        ),
        _FakeCand(
            "a2", section_path="Section A",
            metadata={"source_document_id": "doc-A"},
        ),
    ]
    result = check_pack(
        pack, intent=QueryIntentLabel.RESPONSIBILITY_MAPPING,
        active_document_id="doc-A", active_run_id=None,
    )
    assert not result.ok
    assert "section_diversity_for_structured_intents" in result.failures


def test_check_pack_fails_source_grounding_when_only_enriched():
    pack = [
        _FakeCand(
            "summary", artifact_type="enriched.risks",
            section_path="Risk Register",
            metadata={"source_document_id": "doc-A"},
        ),
    ]
    result = check_pack(
        pack, intent=QueryIntentLabel.ISSUE_RISK_MAPPING,
        active_document_id="doc-A", active_run_id=None,
    )
    # Empty source chunks → fails both grounding AND diversity
    # (single section). The check returns the union; we assert
    # the grounding failure specifically since it's the new check.
    assert not result.ok
    assert (
        "source_grounding_for_enriched_anchored_packs"
        in result.failures
    )


# =====================================================================
# 6. END-TO-END FLOW — happy path emits the full event stream
# =====================================================================
#
# Wires the full chain together on a tiny synthetic corpus to
# prove the diagnostic events fire in the right order with the
# right payload shape. Pins the audit-log invariant the spec
# requires.


def test_full_pipeline_emits_all_stable_event_names(ctx):
    """Smoke: run query → scope → intent → retrieve → rerank →
    dedup → select / drop → finalize. Confirm every stable event
    name appears in the audit stream exactly where the spec
    promises."""
    audit = _SpyAudit()
    diag = RetrievalDiagnostics(
        audit=audit, ctx=ctx, run_id="run-1",
        document_id="doc-A",
        query="Who is responsible for the design review?",
    )
    diag.record_query_received(max_results=10, scope_kind="active")

    candidates = _two_doc_corpus()
    admitted, _ = enforce_active_scope(
        candidates, active_document_id="doc-A",
        active_run_id=None, diagnostics=diag,
    )
    diag.record_scope_applied(
        active_run_id=None, active_document_id="doc-A",
        admitted=len(admitted), rejected=len(candidates) - len(admitted),
        scope_kind="active",
    )
    intent = detect_intent(diag._snapshot.query)
    diag.record_intent_selected(
        intent.intent.value, signals=intent.signals_payload(),
    )

    retrieved = [
        CandidateDiagnostic.from_search_hit(c) for c in admitted
    ]
    diag.record_candidates_retrieved(retrieved, source="bm25")

    # Mock rerank: set rerank_score to inverse of position.
    for i, c in enumerate(retrieved):
        c.rerank_score = float(len(retrieved) - i)
    diag.record_candidates_reranked(retrieved)

    # Dedup: nothing to remove in this fixture.
    diag.record_candidates_deduped(retrieved, removed=[])

    plan = plan_evidence(
        admitted,
        intent=intent.intent,
        max_blocks=5,
    )
    for c in plan.selected:
        d = CandidateDiagnostic.from_search_hit(c)
        diag.record_selected(d, reason="planned")

    pack_check = check_pack(
        plan.selected, intent=intent.intent,
        active_document_id="doc-A", active_run_id=None,
    )
    diag.record_evidence_pack_finalized(
        pack_size=len(plan.selected),
        fallback_triggered=False,
        checks_passed=pack_check.ok,
        check_failures=pack_check.failures,
    )

    actions = [e["action"] for e in audit.events]
    assert EVENT_QUERY_RECEIVED in actions
    assert EVENT_SCOPE_APPLIED in actions
    assert EVENT_INTENT_SELECTED in actions
    assert EVENT_CANDIDATES_RETRIEVED in actions
    assert "j1.retrieval.candidates.reranked" in actions
    assert "j1.retrieval.candidates.deduplicated" in actions
    assert "j1.retrieval.evidence_pack.selected" in actions
    assert "j1.retrieval.evidence_pack.finalized" in actions
    # Drops from the scope filter must also be visible.
    assert EVENT_EVIDENCE_PACK_DROPPED in actions


def test_audit_invariant_every_dropped_candidate_explained(ctx):
    """Spec invariant: every candidate that left ``retrieved``
    without entering ``selected`` must have at least one
    ``evidence_pack.dropped`` event keyed by its ``artifact_id``
    with a non-null ``reason_dropped``."""
    diag = _make_diag(ctx)
    candidates = _two_doc_corpus()  # 5 candidates
    admitted, _ = enforce_active_scope(
        candidates, active_document_id="doc-A",
        active_run_id=None, diagnostics=diag,
    )
    # Only doc-A admitted (a-1, a-2). The 3 doc-B + orphan
    # candidates were dropped during scope enforcement.
    dropped_artifact_ids = {
        c.artifact_id for c in candidates
        if c not in admitted
    }
    drop_events = [
        e for e in diag._audit.events  # type: ignore[union-attr]
        if e["action"] == EVENT_EVIDENCE_PACK_DROPPED
    ]
    drop_event_ids = {
        e["payload"]["artifact_id"] for e in drop_events
    }
    assert dropped_artifact_ids == drop_event_ids
    # Every drop event has a non-null reason.
    assert all(
        e["payload"]["reason_dropped"] is not None
        for e in drop_events
    )
