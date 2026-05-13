"""QueryIntentClassifier — rule-first intent + plan builder.

The classifier reads a question (plus an optional ``DomainProfile``)
and emits a fully-populated ``QueryPlan``. The plan drives every
downstream stage; this module is where every domain-neutral decision
about "what is this question asking" lives.

Design rules:

  1. **Rule-first.** Deterministic regex / pattern matchers fire
     before any LLM. Each intent owns its own ``_classify_*`` helper.
  2. **Confidence reported.** ``Intent.UNKNOWN`` is a valid return —
     callers can choose to invoke an LLM planner when confidence is
     low; this module never calls an LLM on its own.
  3. **Generic over domain-specific.** Stage names, field names, and
     artifact priorities flow in through the ``DomainProfile``.
     Hard-coded surface forms here would (and did) drift away from
     real document vocabulary.
  4. **No retrieval, no synthesis.** This module owns the plan; it
     does not touch the index, the LLM, or the evidence pack. Tests
     can run it on a question string alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from j1.query.domain_profile import DomainProfile, GENERIC_PROFILE
from j1.query.query_plan import (
    AnswerShape,
    EvidenceGroupSpec,
    Intent,
    QualityPolicy,
    QueryPlan,
    RetrievalJob,
    RetrievalRouteKind,
    SufficiencyPolicy,
    SynthesisMode,
)
from j1.retrieval.anchors import (
    query_stage_anchors,
    stage_progression_groups,
)


# ---- Surface patterns -------------------------------------------
#
# Generic across documents. Each pattern lights up ONE signal; the
# classifier combines signals to pick an intent. Order matters for
# stability but each rule is independent — no fall-through magic.

_COMPARISON_VERBS = re.compile(
    r"\b(compare|contrast|differ(?:ence)?|versus|vs\.?|differ\s+from)\b",
    re.IGNORECASE,
)

# "How does X evolve / change / progress through ..." — the stage-
# progression signal. Pairs with stage markers to confirm.
_PROGRESSION_VERBS = re.compile(
    r"\b(evolve|evolves|progress(?:es|ion)?|change\s+through|"
    r"across\s+stages?|from\s+\w+\s+through|"
    r"transition\s+from)\b",
    re.IGNORECASE,
)

# "List / enumerate / what are / which X" — the requirement-style
# extraction signal. The intent collapses with REQUIREMENT_EXTRACTION
# when evidence kinds include ``enriched.requirements``.
_LIST_VERBS = re.compile(
    r"\b(list|enumerate|what\s+are|which\s+\w+\s+are|"
    r"all\s+the|every|each\s+of)\b",
    re.IGNORECASE,
)

_SUMMARY_VERBS = re.compile(
    r"\b(summari[sz]e|overview|high[-\s]?level|brief|in\s+short)\b",
    re.IGNORECASE,
)

_RISK_HEAD = re.compile(
    r"\b(risk|risks|hazard|exposure|mitigation)\b",
    re.IGNORECASE,
)

_REQUIREMENT_HEAD = re.compile(
    r"\b(requirement|requirements|spec|specification|shall\s+|must\s+)\b",
    re.IGNORECASE,
)

_CONSISTENCY_HEAD = re.compile(
    r"\b(consistent|consistency|inconsisten\w*|contradict|conflict|"
    r"disagree|discrepanc)\b",
    re.IGNORECASE,
)

_SCOPE_HEAD = re.compile(
    r"\b(in\s+scope|out\s+of\s+scope|scope\s+of|"
    r"what(?:'s|\s+is)\s+included|exclud)\b",
    re.IGNORECASE,
)

# Citation lookup — "where does it say", "which section", "cite".
_CITATION_HEAD = re.compile(
    r"\b(where\s+does\s+it\s+say|which\s+section|what\s+page|"
    r"cite|citation|reference)\b",
    re.IGNORECASE,
)

# "Deliverable matrix" — the answer-shape ask. When the question
# explicitly asks for a matrix/table of deliverables across a
# dimension (phase, party, etc.), the intent collapses to
# DELIVERABLE_MATRIX even when stage markers are present.
_DELIVERABLE_MATRIX_HEAD = re.compile(
    r"\bdeliverable[s]?\s+(matrix|table|by\s+\w+\s+by\s+\w+)\b",
    re.IGNORECASE,
)

# "X by Y" patterns the comparison detector uses to differentiate
# COMPARISON (two named entities) from MULTI_SECTION_COMPARISON
# (more than two, or grouped by phase/section/role).
_BY_SECTION = re.compile(
    r"\bby\s+(section|phase|stage|chapter|module|role|party)\b",
    re.IGNORECASE,
)

# Field/anchor noun-phrase rough extractor. Used when no domain
# profile is configured — pulls 1-3 word noun phrases that follow
# "the" or sit between verbs / connectors. Cheap and rough; the
# domain profile takes priority when available.
_NOUN_PHRASE = re.compile(
    r"\b(?:the\s+)?([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,2})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClassificationSignal:
    """One firing rule + its confidence contribution. Stored on the
    classifier output so the trace can show *why* an intent was
    chosen."""

    name: str
    matched: bool
    confidence_delta: float = 0.0


class QueryIntentClassifier:
    """Rule-first classifier. Construct once per worker; call
    ``classify(question, profile=...)`` per query."""

    def __init__(
        self,
        *,
        default_max_results: int = 20,
    ) -> None:
        self._default_max_results = default_max_results

    # ---- Public API -------------------------------------------

    def classify(
        self,
        question: str,
        *,
        profile: DomainProfile | None = None,
    ) -> QueryPlan:
        """Return a populated ``QueryPlan`` for the question.

        Always returns a plan — never raises. Unknown intent uses
        ``Intent.UNKNOWN`` with generic retrieval + lenient gates so
        the orchestrator can still attempt an answer."""
        profile = profile or GENERIC_PROFILE
        normalized = _normalise(question)
        intent, confidence = self._detect_intent(normalized, profile)
        anchors = self._extract_anchors(normalized, intent, profile)
        requested_fields = self._extract_fields(
            normalized, intent, profile,
        )
        answer_shape = self._answer_shape(intent, requested_fields)
        synthesis_mode = self._synthesis_mode(intent)
        retrieval_jobs = self._build_retrieval_jobs(
            normalized, intent, anchors, requested_fields, profile,
        )
        required_groups = self._build_required_groups(
            intent, anchors, requested_fields, profile,
        )
        sufficiency = self._sufficiency(intent, profile)
        quality = QualityPolicy(
            required_fields=tuple(requested_fields),
            answer_shape=answer_shape,
            fail_on_refusal=(intent != Intent.CITATION_LOOKUP),
        )
        return QueryPlan(
            normalized_question=normalized,
            intent=intent,
            anchors=tuple(anchors),
            requested_fields=tuple(requested_fields),
            answer_shape=answer_shape,
            synthesis_mode=synthesis_mode,
            retrieval_jobs=tuple(retrieval_jobs),
            required_groups=tuple(required_groups),
            sufficiency=sufficiency,
            quality=quality,
            intent_confidence=confidence,
            domain_id=profile.domain_id,
        )

    # ---- Intent detection -------------------------------------

    def _detect_intent(
        self, question: str, profile: DomainProfile,
    ) -> tuple[Intent, float]:
        """Return ``(intent, confidence)``. Confidence is 1.0 when a
        deterministic rule matched cleanly, lower when multiple
        signals competed, 0.0 for pure fallback."""
        # Specific intents first — they have the strongest signals.
        if _DELIVERABLE_MATRIX_HEAD.search(question):
            return Intent.DELIVERABLE_MATRIX, 0.95
        stage_anchors = query_stage_anchors(question)
        if stage_anchors and (
            _PROGRESSION_VERBS.search(question)
            or len(stage_anchors.stage_markers) >= 2
        ):
            return Intent.STAGE_PROGRESSION, 0.9
        if _CONSISTENCY_HEAD.search(question):
            return Intent.CONSISTENCY_CHECK, 0.85
        if _CITATION_HEAD.search(question):
            return Intent.CITATION_LOOKUP, 0.85
        if _SCOPE_HEAD.search(question):
            return Intent.SCOPE_QUESTION, 0.85
        if _RISK_HEAD.search(question):
            return Intent.RISK_SUMMARY, 0.8
        if _REQUIREMENT_HEAD.search(question) and _LIST_VERBS.search(question):
            return Intent.REQUIREMENT_EXTRACTION, 0.85
        if _BY_SECTION.search(question) and _COMPARISON_VERBS.search(question):
            return Intent.MULTI_SECTION_COMPARISON, 0.85
        if _COMPARISON_VERBS.search(question):
            return Intent.COMPARISON, 0.8
        if _SUMMARY_VERBS.search(question):
            return Intent.SUMMARY, 0.75
        # Plain "what is X" / "who is X" / single-token answer expected.
        if re.match(
            r"^\s*(what|who|when|where|how\s+many|how\s+much)\b",
            question, re.IGNORECASE,
        ):
            return Intent.SINGLE_FACT, 0.7
        return Intent.UNKNOWN, 0.2

    # ---- Anchor extraction -----------------------------------

    def _extract_anchors(
        self,
        question: str,
        intent: Intent,
        profile: DomainProfile,
    ) -> list[str]:
        """Generic anchor extraction. For stage_progression we use
        ``stage_progression_groups`` (cleanly separates true stages
        from estimate-class markers). For other intents we surface-
        match domain-profile aliases the question mentions."""
        anchors: list[str] = []
        seen: set[str] = set()

        def _add(value: str) -> None:
            v = value.strip()
            if not v:
                return
            k = v.lower()
            if k in seen:
                return
            seen.add(k)
            anchors.append(v)

        if intent == Intent.STAGE_PROGRESSION:
            # True stages only — ``stage_progression_groups``
            # excludes estimate-class markers that aren't actual
            # stages.
            sg = stage_progression_groups(question)
            if sg:
                for stage in sg.stages_requested:
                    canonical = profile.stage_canonical(stage)
                    _add(canonical or stage)
            return anchors

        # For other intents, surface-match domain stages/fields the
        # user mentioned.
        ql = question.lower()
        for stage in profile.stages:
            for alias in (stage.canonical, *stage.aliases):
                if alias.lower() in ql:
                    _add(stage.canonical)
                    break
        for field in profile.fields:
            for alias in (field.canonical, *field.aliases):
                if alias.lower() in ql:
                    _add(field.canonical)
                    break
        return anchors

    # ---- Field extraction ------------------------------------

    def _extract_fields(
        self,
        question: str,
        intent: Intent,
        profile: DomainProfile,
    ) -> list[str]:
        """Pick out the *requested fields* — the columns the answer
        must populate.

        Order of evidence:
          1. Domain-profile canonical names whose aliases appear in
             the question (cleanest).
          2. Estimate-class detection (``cost estimate class`` is a
             special anchor; promote it to a requested field).
          3. Progression-term nouns the user wrote alongside stage
             markers — minus generic stop tokens.
          4. As a last resort, bounded "the X" noun phrases.
        """
        fields: list[str] = []
        seen: set[str] = set()

        def _add(value: str) -> None:
            v = value.strip()
            if not v:
                return
            k = v.lower()
            if k in seen:
                return
            seen.add(k)
            fields.append(v)

        ql = question.lower()
        for field in profile.fields:
            for alias in (field.canonical, *field.aliases):
                if alias.lower() in ql:
                    _add(field.canonical)
                    break

        # ``query_stage_anchors`` already returns the user's
        # estimate-class anchor as a marker — promote it to a
        # field so the synthesizer has a column to fill.
        stage_anchors = query_stage_anchors(question)
        for marker in stage_anchors.stage_markers:
            ml = marker.lower()
            if (
                "estimate" in ml
                and ("class" in ml or "classification" in ml)
            ):
                _add(marker)

        # Progression terms minus generic ones — "deliverables" is
        # a field; "design" is not (it's the stage marker context).
        _STAGE_CONTEXT_NOUNS = {"design", "review", "approval"}
        for term in stage_anchors.progression_terms:
            tl = term.lower()
            if tl in _STAGE_CONTEXT_NOUNS:
                continue
            _add(term)

        # Last resort: bounded noun phrase after "the", limited to
        # 1-2 words and cut off at a verb / stop token.
        if not fields:
            for m in re.finditer(
                r"\bthe\s+([a-z][a-z\-]+(?:\s+[a-z][a-z\-]+)?)\b",
                question, re.IGNORECASE,
            ):
                phrase = m.group(1).strip().lower()
                if (
                    phrase in _STOP_PHRASES
                    or phrase in {
                        "question", "answer", "document",
                        "section", "report", "project", "run", "doc",
                    }
                ):
                    continue
                # Skip phrases that contain a verb-y token.
                if any(
                    word in {"evolve", "is", "are", "was",
                             "associated", "do", "does"}
                    for word in phrase.split()
                ):
                    continue
                _add(phrase)
        return fields

    # ---- Answer shape ----------------------------------------

    def _answer_shape(
        self,
        intent: Intent,
        requested_fields: list[str],
    ) -> AnswerShape:
        mapping = {
            Intent.STAGE_PROGRESSION: AnswerShape.STAGE_BY_STAGE_TABLE,
            Intent.DELIVERABLE_MATRIX: AnswerShape.DELIVERABLE_MATRIX,
            Intent.COMPARISON: AnswerShape.SIDE_BY_SIDE_TABLE,
            Intent.MULTI_SECTION_COMPARISON: AnswerShape.SIDE_BY_SIDE_TABLE,
            Intent.REQUIREMENT_EXTRACTION: AnswerShape.REQUIREMENT_LIST,
            Intent.RISK_SUMMARY: AnswerShape.RISK_LIST,
            Intent.SUMMARY: AnswerShape.PARAGRAPH,
            Intent.SINGLE_FACT: AnswerShape.SHORT_FACT,
            Intent.CITATION_LOOKUP: AnswerShape.SHORT_FACT,
            Intent.SCOPE_QUESTION: AnswerShape.PARAGRAPH,
            Intent.CONSISTENCY_CHECK: AnswerShape.BULLET_LIST,
        }
        return mapping.get(intent, AnswerShape.PARAGRAPH)

    # ---- Synthesis mode --------------------------------------

    def _synthesis_mode(self, intent: Intent) -> SynthesisMode:
        """Pick how much inference the synthesizer is allowed.

        For intents where missing values must NOT be filled in by
        the LLM (cost classes, risk numbers), we use EXTRACT_ONLY.
        For prose questions (summary), SYNTHESIZE."""
        if intent in {
            Intent.STAGE_PROGRESSION,
            Intent.DELIVERABLE_MATRIX,
            Intent.REQUIREMENT_EXTRACTION,
            Intent.RISK_SUMMARY,
            Intent.CITATION_LOOKUP,
            Intent.CONSISTENCY_CHECK,
        }:
            return SynthesisMode.PROJECT_STRUCTURED
        if intent == Intent.SINGLE_FACT:
            return SynthesisMode.EXTRACT_ONLY
        return SynthesisMode.SYNTHESIZE

    # ---- Retrieval jobs --------------------------------------

    def _build_retrieval_jobs(
        self,
        question: str,
        intent: Intent,
        anchors: list[str],
        requested_fields: list[str],
        profile: DomainProfile,
    ) -> list[RetrievalJob]:
        jobs: list[RetrievalJob] = []
        # Primary: RAGAnything (semantic + graph) over the user's
        # question.
        jobs.append(RetrievalJob(
            route=RetrievalRouteKind.RAGANYTHING,
            query=question,
            max_results=self._default_max_results,
            label="primary",
        ))
        # BM25 lexical recall over the FULL question text. Always
        # dispatched, not anchor-only — for queries with no detected
        # anchors (e.g. plain ``what is X?``) BM25 is the only route
        # that hits the lexical index. Without this job, a thin
        # RAGAnything response leaves the pack empty and the
        # sufficiency gate fails before the LLM is called.
        jobs.append(RetrievalJob(
            route=RetrievalRouteKind.BM25,
            query=question,
            max_results=self._default_max_results,
            label="bm25_primary",
        ))
        # Additional BM25 jobs per anchor term. The user writing
        # "60% design" is a strong lexical signal that exact-phrase
        # recall will help — even when the semantic retriever didn't
        # surface those chunks at top-K.
        for anchor in anchors:
            jobs.append(RetrievalJob(
                route=RetrievalRouteKind.BM25,
                query=anchor,
                max_results=10,
                label=f"bm25_anchor:{anchor}",
            ))
        # Artifact-lookup route for fields backed by enriched
        # overlays. For requirement_extraction this typically wires
        # to ``enriched.requirements``; for risk_summary to
        # ``enriched.risks``.
        for fname in requested_fields:
            kinds = profile.field_artifact_kinds(fname)
            for kind in kinds:
                jobs.append(RetrievalJob(
                    route=RetrievalRouteKind.ARTIFACT_LOOKUP,
                    query=fname,
                    max_results=20,
                    filters={"artifact_kind": kind},
                    label=f"artifact:{kind}",
                ))
        # Some intents always pull from a canonical enriched kind.
        if intent == Intent.REQUIREMENT_EXTRACTION:
            jobs.append(RetrievalJob(
                route=RetrievalRouteKind.ARTIFACT_LOOKUP,
                query=question,
                max_results=50,
                filters={"artifact_kind": "enriched.requirements"},
                label="artifact:enriched.requirements",
            ))
        if intent == Intent.RISK_SUMMARY:
            jobs.append(RetrievalJob(
                route=RetrievalRouteKind.ARTIFACT_LOOKUP,
                query=question,
                max_results=50,
                filters={"artifact_kind": "enriched.risks"},
                label="artifact:enriched.risks",
            ))
        if intent == Intent.CONSISTENCY_CHECK:
            jobs.append(RetrievalJob(
                route=RetrievalRouteKind.ARTIFACT_LOOKUP,
                query=question,
                max_results=50,
                filters={"artifact_kind": "enriched.consistency_findings"},
                label="artifact:enriched.consistency_findings",
            ))
        return jobs

    # ---- Required groups -------------------------------------

    def _build_required_groups(
        self,
        intent: Intent,
        anchors: list[str],
        requested_fields: list[str],
        profile: DomainProfile,
    ) -> list[EvidenceGroupSpec]:
        """Materialise the groups the evidence pack must populate.

        For stage_progression the canonical recipe is one group per
        stage marker + one group per requested field. Other intents
        produce a single "answer" group unless they have their own
        structural rule."""
        groups: list[EvidenceGroupSpec] = []
        seen: set[str] = set()

        def _add(group: EvidenceGroupSpec) -> None:
            key = group.name.lower()
            if key in seen:
                return
            seen.add(key)
            groups.append(group)

        if intent == Intent.STAGE_PROGRESSION:
            # ``anchors`` already contains the clean stage list
            # (built via ``stage_progression_groups`` upstream). Don't
            # re-parse — the join "60% 90% 100% design" would let
            # the percentage regex's "(?:\s+\w+)?" capture cross-
            # boundary tokens (e.g. "60% 90").
            for stage in anchors:
                _add(EvidenceGroupSpec(
                    name=stage,
                    description=f"Evidence describing {stage}.",
                    anchors=(stage,),
                    required=True,
                ))
            for field in requested_fields:
                _add(EvidenceGroupSpec(
                    name=field,
                    description=f"Evidence describing {field}.",
                    anchors=(field,),
                    required=True,
                ))
        elif intent == Intent.DELIVERABLE_MATRIX:
            for anchor in anchors:
                _add(EvidenceGroupSpec(
                    name=anchor,
                    description=f"Deliverable evidence for {anchor}.",
                    anchors=(anchor,),
                    required=True,
                ))
        elif intent == Intent.COMPARISON:
            # The two anchors in the comparison become the groups
            # (one column each).
            for anchor in anchors[:2]:
                _add(EvidenceGroupSpec(
                    name=anchor, anchors=(anchor,), required=True,
                ))
        else:
            _add(EvidenceGroupSpec(
                name="answer",
                description="General evidence for the question.",
                required=True,
            ))
        return groups

    # ---- Sufficiency policy ---------------------------------

    def _sufficiency(
        self, intent: Intent, profile: DomainProfile,
    ) -> SufficiencyPolicy:
        # Defaults by intent.
        if intent == Intent.STAGE_PROGRESSION:
            policy = SufficiencyPolicy(
                # At least 3 of the requested stage groups must
                # have evidence (matches the failed-question spec).
                min_required_groups=3,
                min_total_blocks=3,
                fail_when_no_candidates=True,
            )
        elif intent in {
            Intent.DELIVERABLE_MATRIX,
            Intent.COMPARISON,
            Intent.MULTI_SECTION_COMPARISON,
        }:
            policy = SufficiencyPolicy(
                min_required_groups=2,
                min_total_blocks=2,
                fail_when_no_candidates=True,
            )
        elif intent in {
            Intent.REQUIREMENT_EXTRACTION,
            Intent.RISK_SUMMARY,
            Intent.CONSISTENCY_CHECK,
        }:
            policy = SufficiencyPolicy(
                min_required_groups=1,
                min_total_blocks=1,
                fail_when_no_candidates=True,
            )
        elif intent == Intent.CITATION_LOOKUP:
            # "Not found" is a legitimate answer; don't fail on
            # zero candidates.
            policy = SufficiencyPolicy(
                min_required_groups=0,
                min_total_blocks=0,
                fail_when_no_candidates=False,
            )
        else:
            policy = SufficiencyPolicy(
                min_required_groups=1,
                min_total_blocks=1,
                fail_when_no_candidates=True,
            )
        # Apply profile-level overrides last so a domain can
        # tighten or loosen the defaults without forking the
        # classifier.
        overrides = profile.sufficiency_overrides.get(intent, {})
        if overrides:
            kwargs = {
                "min_required_groups": policy.min_required_groups,
                "min_total_blocks": policy.min_total_blocks,
                "fail_when_no_candidates": (
                    policy.fail_when_no_candidates
                ),
            }
            for k, v in overrides.items():
                if k in kwargs:
                    kwargs[k] = v
            policy = SufficiencyPolicy(**kwargs)
        return policy


# ---- Helpers ----------------------------------------------------


_STOP_PHRASES: frozenset[str] = frozenset({
    "associated with each",
    "associated with",
    "each design stage",
    "each stage",
    "and which",
    "which",
    "how",
    "what",
    "design stage",
    "design stages",
})


def _normalise(question: str) -> str:
    """Light normalisation — collapse whitespace, strip outer
    punctuation. We deliberately keep the user's casing because
    proper nouns (section titles, doc names) carry signal."""
    if not question:
        return ""
    s = question.strip()
    s = re.sub(r"\s+", " ", s)
    return s


__all__ = [
    "ClassificationSignal",
    "QueryIntentClassifier",
]
