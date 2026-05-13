"""Retrieval-quality instrumentation + planning surface.

Module layout (all generic, document-agnostic):

  * ``diagnostics`` — 9 stable structured events that explain
    every candidate's journey from raw retrieval to the evidence
    pack sent to the LLM. The audit trail answers "why was a
    relevant candidate dropped?" without any guesswork.

  * ``scope`` — strict active-document / active-run filter
    applied BEFORE intent routing or rerank. Every candidate is
    annotated with scope metadata; any candidate that fails the
    active-scope check is dropped + logged.

  * ``intent_router`` — deterministic, keyword + verb based
    classifier into 16 GENERIC intents. No domain lexicons
    (insurance / contract are the only sector-named intents, and
    only because their request-shape is identical across domains).

  * ``boilerplate`` — generic pattern matcher for standard
    contract / template / signature / notices sections, with
    intent-aware demotion (legal/compliance intents skip
    demotion entirely).

  * ``evidence_planner`` — structure-aware evidence selection.
    Reads the document's heading hierarchy / section diversity /
    artifact-type mix from the candidate metadata and plans the
    pack accordingly. No hard-coded section labels.

  * ``quality_checks`` — pre-LLM verification of the planned
    pack: non-empty, single-scope, no-boilerplate-when-intent-
    forbids, section diversity for structured intents. Failure
    triggers ONE fallback retrieval pass with adjusted
    parameters; if still failing the API returns an explicit
    insufficient-evidence state instead of synthesising a
    confident but unfounded answer.

Wiring contract: every module is OPTIONAL and the existing
retrieval pipeline runs unchanged when ``RetrievalDiagnostics`` is
``None``. The new code is a side-channel — the legacy reranker /
evidence selector still drives behaviour until the wiring patch
elsewhere flips it on.
"""

from j1.retrieval.diagnostics import (
    CandidateDiagnostic,
    DropReason,
    EVENT_CANDIDATES_DEDUPED,
    EVENT_CANDIDATES_RERANKED,
    EVENT_CANDIDATES_RETRIEVED,
    EVENT_EVIDENCE_PACK_DROPPED,
    EVENT_EVIDENCE_PACK_FINALIZED,
    EVENT_EVIDENCE_PACK_SELECTED,
    EVENT_INTENT_SELECTED,
    EVENT_QUERY_RECEIVED,
    EVENT_SCOPE_APPLIED,
    RetrievalDiagnostics,
)
from j1.retrieval.intent_router import (
    IntentDetection,
    QueryIntentLabel,
    detect_intent,
)
from j1.retrieval.boilerplate import (
    BoilerplateCategory,
    BoilerplateMatch,
    boilerplate_demotion,
    is_boilerplate_chunk,
)
from j1.retrieval.scope import (
    CandidateScope,
    ScopeViolation,
    annotate_scope,
    enforce_active_scope,
)
from j1.retrieval.evidence_planner import (
    PlannedEvidence,
    PlannerOutcome,
    plan_evidence,
)
from j1.retrieval.quality_checks import (
    EvidenceCheckResult,
    check_pack,
)

__all__ = [
    "BoilerplateCategory",
    "BoilerplateMatch",
    "CandidateDiagnostic",
    "CandidateScope",
    "DropReason",
    "EVENT_CANDIDATES_DEDUPED",
    "EVENT_CANDIDATES_RERANKED",
    "EVENT_CANDIDATES_RETRIEVED",
    "EVENT_EVIDENCE_PACK_DROPPED",
    "EVENT_EVIDENCE_PACK_FINALIZED",
    "EVENT_EVIDENCE_PACK_SELECTED",
    "EVENT_INTENT_SELECTED",
    "EVENT_QUERY_RECEIVED",
    "EVENT_SCOPE_APPLIED",
    "EvidenceCheckResult",
    "IntentDetection",
    "PlannedEvidence",
    "PlannerOutcome",
    "QueryIntentLabel",
    "RetrievalDiagnostics",
    "ScopeViolation",
    "annotate_scope",
    "boilerplate_demotion",
    "check_pack",
    "detect_intent",
    "enforce_active_scope",
    "is_boilerplate_chunk",
    "plan_evidence",
]
