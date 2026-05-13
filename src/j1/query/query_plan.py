"""QueryPlan + shared shapes the orchestrator builds before retrieval.

The plan is the contract between intent classification and every downstream
component:

  Intent classifier  →  QueryPlan  →  retrieval routes
                                  ↘ evidence selection (required groups)
                                  ↘ sufficiency gate (thresholds)
                                  ↘ synthesizer (answer shape)
                                  ↘ quality gate (required fields)

The plan also drives the manual-test view — operators see "what was asked,
what was planned, what was retrieved" without having to reverse-engineer
intent from the answer.

Everything in this module is a plain dataclass with no domain vocabulary.
Domain-specific knowledge (stage names, vocabulary, thresholds) lives in
``domain_profile.DomainProfile`` and is *consulted* by the planner; never
hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Intent(StrEnum):
    """Broad query intent taxonomy. Stable strings — log consumers and
    UI surfaces match against these names directly.

    The list intentionally stays narrow. New intents land here when a
    distinct retrieval shape AND a distinct sufficiency policy AND a
    distinct answer shape can be defined for them. Otherwise the query
    folds into the closest existing intent and the domain profile
    handles the nuance.
    """

    SINGLE_FACT = "single_fact"
    SUMMARY = "summary"
    COMPARISON = "comparison"
    MULTI_SECTION_COMPARISON = "multi_section_comparison"
    STAGE_PROGRESSION = "stage_progression"
    REQUIREMENT_EXTRACTION = "requirement_extraction"
    RISK_SUMMARY = "risk_summary"
    DELIVERABLE_MATRIX = "deliverable_matrix"
    CONSISTENCY_CHECK = "consistency_check"
    SCOPE_QUESTION = "scope_question"
    CITATION_LOOKUP = "citation_lookup"
    UNKNOWN = "unknown"


class AnswerShape(StrEnum):
    """How the synthesizer must structure the final answer.

    ``stage_by_stage_table`` is the load-bearing one for the failed
    test query — the synthesizer renders rows keyed by stage with
    columns for the requested fields. The quality gate verifies the
    structure rather than just non-empty prose."""

    PARAGRAPH = "paragraph"
    BULLET_LIST = "bullet_list"
    SHORT_FACT = "short_fact"
    STAGE_BY_STAGE_TABLE = "stage_by_stage_table"
    SIDE_BY_SIDE_TABLE = "side_by_side_table"
    REQUIREMENT_LIST = "requirement_list"
    RISK_LIST = "risk_list"
    DELIVERABLE_MATRIX = "deliverable_matrix"


class SynthesisMode(StrEnum):
    """Whether the LLM is asked to synthesize prose or to project
    structured data with no inference. ``EXTRACT_ONLY`` is the safer
    mode for legal / cost / risk questions — the LLM may only quote;
    it may not infer missing values."""

    EXTRACT_ONLY = "extract_only"
    SYNTHESIZE = "synthesize"
    PROJECT_STRUCTURED = "project_structured"


class RetrievalRouteKind(StrEnum):
    """Routes the orchestrator can dispatch in parallel.

    ``RAGANYTHING`` is the primary (semantic + graph) backend. ``BM25``
    is a lexical recall route — auxiliary, never authoritative.
    ``ARTIFACT_LOOKUP`` reads enriched artifacts directly without
    going through an LLM-mediated retriever. ``NEARBY_SECTION``
    expands the section around a previously-matched chunk."""

    RAGANYTHING = "raganything"
    BM25 = "bm25"
    ARTIFACT_LOOKUP = "artifact_lookup"
    NEARBY_SECTION = "nearby_section"


@dataclass(frozen=True)
class RetrievalJob:
    """One concrete retrieval to dispatch. ``query`` is the route-
    specific query string (often the user question, sometimes
    expanded with synonyms or anchored phrases). ``filters`` is
    optional metadata for the adapter — e.g. ``{"artifact_kind":
    "enriched.requirements"}``."""

    route: RetrievalRouteKind
    query: str
    max_results: int = 20
    filters: dict[str, Any] = field(default_factory=dict)
    # Free-text label for the trace ("primary", "bm25_anchor:60%
    # design", "artifact:enriched.requirements"). Operators read this
    # in the manual view to understand WHY the route ran.
    label: str = ""


@dataclass(frozen=True)
class EvidenceGroupSpec:
    """A logical bucket the evidence builder must fill.

    For ``stage_progression`` the planner produces one group per stage
    plus one per requested field. The sufficiency gate then asserts a
    minimum number of populated groups; the synthesizer renders the
    answer row-by-group."""

    name: str
    # Human-readable description for the manual view.
    description: str = ""
    # Anchor phrases that flag a chunk as belonging to this group.
    # Generic over domain-specific: the planner pulls anchors from the
    # query text + domain profile, never from a hard-coded list here.
    anchors: tuple[str, ...] = ()
    # Whether the group is required for the sufficiency gate to pass.
    # Non-required groups feed the answer but don't block on absence.
    required: bool = True


@dataclass(frozen=True)
class SufficiencyPolicy:
    """Intent-specific evidence sufficiency thresholds.

    The gate consults these values to decide ``evidence_insufficient``
    vs ``ok``. Values are intentionally explicit (not derived) so an
    operator reading the trace can answer "why did this fail" without
    running the gate again.
    """

    # Minimum number of required groups that must have at least one
    # block. For stage_progression this is "3 of the 4 stages".
    min_required_groups: int = 1
    # Minimum block count across all groups.
    min_total_blocks: int = 1
    # When False, an intent with ZERO retrieved candidates still
    # reaches the synthesizer (e.g. CITATION_LOOKUP returning "no
    # match" is a valid answer). Default is True — most intents
    # require evidence to answer.
    fail_when_no_candidates: bool = True


@dataclass(frozen=True)
class QualityPolicy:
    """Intent-specific answer-quality rules.

    Replaces the legacy length heuristic — the quality gate checks
    the explicit list of required fields, the answer shape, and the
    refusal pattern. No "long answer means substantive" shortcut.
    """

    required_fields: tuple[str, ...] = ()
    answer_shape: AnswerShape = AnswerShape.PARAGRAPH
    # When True, the gate fails if the answer matches a refusal /
    # "not in evidence" pattern. Set to False only for intents where
    # "not found" is a legitimate substantive answer (citation
    # lookups against an empty active scope).
    fail_on_refusal: bool = True


@dataclass(frozen=True)
class QueryPlan:
    """The structured plan the orchestrator hands to every downstream
    stage. Serialisable so it can land verbatim in the QueryTrace and
    in the manual-test view JSON.

    The plan is built once per query — every later stage reads from
    it, no stage mutates it. That keeps the trace honest: the plan
    you see in the manual view is exactly what drove retrieval.
    """

    normalized_question: str
    intent: Intent
    anchors: tuple[str, ...]
    requested_fields: tuple[str, ...]
    answer_shape: AnswerShape
    synthesis_mode: SynthesisMode
    retrieval_jobs: tuple[RetrievalJob, ...]
    required_groups: tuple[EvidenceGroupSpec, ...]
    sufficiency: SufficiencyPolicy
    quality: QualityPolicy
    # Confidence the classifier had in the assigned intent — drives
    # whether the LLM planner re-ran. 1.0 = deterministic rule
    # matched; 0.0 = pure fallback. Surfaced for trace visibility only.
    intent_confidence: float = 1.0
    # Domain-profile id consulted during planning. Empty string when
    # no domain profile applied (generic mode).
    domain_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON shape for the trace + manual view. Keys match
        the spec's example plan; new fields land at the end."""
        return {
            "normalized_question": self.normalized_question,
            "intent": self.intent.value,
            "anchors": list(self.anchors),
            "requested_fields": list(self.requested_fields),
            "answer_shape": self.answer_shape.value,
            "synthesis_mode": self.synthesis_mode.value,
            "retrieval_jobs": [
                {
                    "route": j.route.value,
                    "query": j.query,
                    "max_results": j.max_results,
                    "filters": dict(j.filters),
                    "label": j.label,
                }
                for j in self.retrieval_jobs
            ],
            "required_groups": [
                {
                    "name": g.name,
                    "description": g.description,
                    "anchors": list(g.anchors),
                    "required": g.required,
                }
                for g in self.required_groups
            ],
            "sufficiency": {
                "min_required_groups": self.sufficiency.min_required_groups,
                "min_total_blocks": self.sufficiency.min_total_blocks,
                "fail_when_no_candidates": (
                    self.sufficiency.fail_when_no_candidates
                ),
            },
            "quality": {
                "required_fields": list(self.quality.required_fields),
                "answer_shape": self.quality.answer_shape.value,
                "fail_on_refusal": self.quality.fail_on_refusal,
            },
            "intent_confidence": self.intent_confidence,
            "domain_id": self.domain_id,
        }


# ---- Evidence shapes ---------------------------------------------


@dataclass(frozen=True)
class EvidenceCandidate:
    """One raw retrieval hit. Produced by a route adapter; consumed
    by the EvidencePackBuilder which dedupes, groups, ranks, and
    selects."""

    route: RetrievalRouteKind
    artifact_id: str
    artifact_kind: str
    chunk_id: str | None
    text_preview: str
    score: float
    matched_anchors: tuple[str, ...]
    run_id: str | None
    document_id: str | None
    project_id: str
    # Free-form route metadata kept for the trace ("section_path",
    # "lightrag_node_id", "bm25_term_hits"). Not consumed by gates.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.value,
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "chunk_id": self.chunk_id,
            "text_preview": self.text_preview,
            "score": self.score,
            "matched_anchors": list(self.matched_anchors),
            "run_id": self.run_id,
            "document_id": self.document_id,
            "project_id": self.project_id,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class EvidenceBlock:
    """A selected piece of evidence — the thing the synthesizer is
    actually allowed to read. ``group`` is the EvidenceGroupSpec.name
    this block contributes to (may be unassigned for free evidence)."""

    candidate: EvidenceCandidate
    body: str
    group: str | None
    # Rank within the group after the pack builder ordered things.
    rank_in_group: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "body": self.body,
            "group": self.group,
            "rank_in_group": self.rank_in_group,
        }


@dataclass(frozen=True)
class DroppedCandidate:
    """A candidate the evidence builder rejected. The reason is the
    most useful thing in the manual view: "why didn't this chunk
    make it into evidence?" is the question operators ask first."""

    candidate: EvidenceCandidate
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EvidencePack:
    """Output of the EvidencePackBuilder. ``blocks`` is what the
    synthesizer reads; ``groups_covered`` / ``groups_missing`` drive
    the sufficiency gate; ``dropped`` populates the trace."""

    blocks: tuple[EvidenceBlock, ...]
    groups_covered: tuple[str, ...]
    groups_missing: tuple[str, ...]
    dropped: tuple[DroppedCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [b.to_dict() for b in self.blocks],
            "groups_covered": list(self.groups_covered),
            "groups_missing": list(self.groups_missing),
            "dropped": [d.to_dict() for d in self.dropped],
        }


# ---- Gate result -------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """One row in the gate-results table. ``passed`` is the gate's
    boolean; ``severity`` is "required" (must pass for ok status) or
    "advisory" (informational). ``reason`` is the operator-visible
    explanation — required when ``passed=False``."""

    name: str
    passed: bool
    severity: str = "required"
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


__all__ = [
    "AnswerShape",
    "DroppedCandidate",
    "EvidenceBlock",
    "EvidenceCandidate",
    "EvidenceGroupSpec",
    "EvidencePack",
    "GateResult",
    "Intent",
    "QualityPolicy",
    "QueryPlan",
    "RetrievalJob",
    "RetrievalRouteKind",
    "SufficiencyPolicy",
    "SynthesisMode",
]
