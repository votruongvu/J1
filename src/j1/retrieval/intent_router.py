"""Deterministic generic intent router.

The 16 intents in this file are GENERIC analytical shapes — never
domain-specific. The router decides what shape the evidence pack
needs (a flat list, a stage-progression, a who→what map, a
risk→action map, etc.) so the planner can structure the pack
accordingly. It does NOT classify documents or sectors.

Routing signal set:

  * **Verbs.** ``list``, ``compare``, ``map``, ``relate``,
    ``depend``, ``require``, ``support``, ``reduce``, ``manage``,
    ``decide``, ``produce``, ``evolve``, ``progress``.
  * **Output-shape cues.** ``how do X evolve``,
    ``which X feed into Y``, ``who is responsible for``,
    ``what are the steps``, ``which X depends on Y``,
    ``list all``, ``compare X and Y``.
  * **Sector-shaped intents** (legal, insurance, compliance,
    cost, schedule) only fire when the query EXPLICITLY uses
    that vocabulary. They exist so the boilerplate filter knows
    when NOT to demote a legal/insurance chunk.

The router is deterministic (regex-based), explainable, fast.
Returns ``GENERIC_LOOKUP`` when nothing fires above threshold —
the reranker's default top-K-by-coverage logic applies.

This file is the ONLY place new intent labels should be added.
Adding here means:
  1. Append to the ``QueryIntentLabel`` enum.
  2. Add the lexicon entry to ``_INTENT_LEXICONS``.
  3. The planner's per-intent rules pick up the new label
     through ``QueryIntentLabel`` — no other module changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class QueryIntentLabel(StrEnum):
    """The 16 generic retrieval intents. Stable string values —
    audit consumers key against them."""

    GENERIC_LOOKUP = "generic_lookup"
    SUMMARY_LOOKUP = "summary_lookup"
    EXACT_FACT_LOOKUP = "exact_fact_lookup"
    LIST_EXTRACTION = "list_extraction"
    REQUIREMENTS_LOOKUP = "requirements_lookup"
    RESPONSIBILITY_MAPPING = "responsibility_mapping"
    DEPENDENCY_MAPPING = "dependency_mapping"
    STAGE_PROGRESSION = "stage_progression"
    DELIVERABLE_MAPPING = "deliverable_mapping"
    ISSUE_RISK_MAPPING = "issue_risk_mapping"
    DECISION_TRACE = "decision_trace"
    COMPARISON = "comparison"
    COMPLIANCE_LOOKUP = "compliance_lookup"
    LEGAL_OR_CONTRACT_TERMS = "legal_or_contract_terms"
    COST_OR_EFFORT_LOOKUP = "cost_or_effort_lookup"
    SCHEDULE_OR_MILESTONE_LOOKUP = "schedule_or_milestone_lookup"


# ---- Lexicons ----------------------------------------------------
#
# Each entry: (regex pattern, weight). Word boundaries (\b) avoid
# substring noise. Weights ~0.5 are weak signals (a single match
# alone doesn't qualify); weights ≥2.0 are strong (one match is
# enough to clear the threshold).

_LIST_EXTRACTION: list[tuple[str, float]] = [
    (r"\blist\b", 2.0),
    (r"\benumerate\b", 2.0),
    (r"\b(what|which)\s+(are|is)\s+the\b", 1.0),
    (r"\bitemiz(e|ed)\b", 2.0),
    (r"\ball\s+(of\s+)?(the\s+)?", 0.8),
    (r"\beach\s+", 0.5),
]

_SUMMARY_LOOKUP: list[tuple[str, float]] = [
    (r"\bsummariz(e|ation|ed)\b", 2.5),
    (r"\bsummary\b", 2.5),
    (r"\boverview\b", 2.0),
    (r"\bwhat\s+is\s+(this\s+)?(document|the\s+document)\s+about\b", 3.0),
    (r"\btell\s+me\s+about\b", 1.5),
    (r"\bin\s+brief\b", 1.5),
    (r"\bdescribe\b", 1.0),
]

_EXACT_FACT_LOOKUP: list[tuple[str, float]] = [
    (r"\bwhat\s+is\s+the\s+", 1.2),
    (r"\bwhen\s+is\b", 1.5),
    (r"\bwhere\s+is\b", 1.5),
    (r"\bwho\s+is\b", 1.0),
    (r"\bhow\s+many\b", 2.0),
    (r"\bhow\s+much\b", 1.5),
    (r"\bvalue\s+of\s+", 1.0),
]

_REQUIREMENTS: list[tuple[str, float]] = [
    (r"\brequirement[s]?\b", 2.5),
    (r"\bshall\s+\w+", 1.5),
    (r"\b(must|will)\s+(provide|comply|deliver|meet)\b", 1.5),
    (r"\bobligation[s]?\b", 2.0),
    (r"\bmandator(y|ily)\b", 2.0),
    (r"\bcompliance\s+criteri", 1.5),
    (r"\bspecification[s]?\b", 1.5),
]

_RESPONSIBILITY: list[tuple[str, float]] = [
    (r"\bwho\s+(is|are)\s+responsible\b", 3.0),
    (r"\bresponsibilit(y|ies)\b", 2.0),
    (r"\bresponsible\s+for\b", 2.0),
    (r"\bowner[s]?\s+of\b", 1.5),
    (r"\bowns\b", 1.0),
    (r"\bperforms?\b", 1.0),
    (r"\bcarries?\s+out\b", 1.0),
    (r"\bin\s+charge\s+of\b", 1.5),
    (r"\bRACI\b", 3.0),
    (r"\b(approve|review)s?\b", 0.5),
    (r"\b(produce|coordinate|support|lead)s?\b", 0.5),
]

_DEPENDENCY: list[tuple[str, float]] = [
    (r"\bdepend(s|ed|ing|ency|encies)?\s+(on|upon|between)\b", 3.0),
    (r"\bfeed[s]?\s+into\b", 2.5),
    (r"\bbuilds?\s+(on|upon)\b", 1.5),
    (r"\bbased\s+on\b", 1.0),
    (r"\b(supports?|enables?|constrains?)\b", 1.0),
    (r"\b(input|output)\s+to\b", 1.5),
    (r"\b(precondition|prerequisite)s?\b", 2.0),
    (r"\bdata\s+flow\b", 1.5),
    (r"\bupstream\b", 1.0),
    (r"\bdownstream\b", 1.0),
    (r"\brelate[s]?\s+to\b", 0.8),
    (r"\bbetween\s+\w+\s+and\b", 0.5),
]

_STAGE_PROGRESSION: list[tuple[str, float]] = [
    (r"\bstage[s]?\b", 1.5),
    (r"\bphase[s]?\b", 1.5),
    (r"\b(evolves?|evolution|progress(es|ion)?)\b", 2.0),
    (r"\bfrom\s+\w+\s+(to|through)\s+\w+\b", 1.5),
    (r"\bhow\s+do(es)?\s+\w+\s+(evolve|progress|change)", 2.5),
    (r"\bmilestone[s]?\b", 1.0),
    (r"\bstep[s]?\b", 0.8),
    (r"\biteration[s]?\b", 1.0),
    (r"\b(over|across)\s+(time|phases?|stages?)\b", 1.0),
]

_DELIVERABLE: list[tuple[str, float]] = [
    (r"\bdeliverable[s]?\b", 2.5),
    (r"\boutput[s]?\b", 1.0),
    (r"\bproduce[s]?\b.*\b(report|document|plan)\b", 1.5),
    (r"\bartifact[s]?\b", 1.5),
    (r"\bsubmittal[s]?\b", 2.0),
    (r"\b(report|document|memo|spec)s?\s+to\s+be\s+(produced|delivered)\b", 2.0),
    (r"\bfinal\s+(report|deliverable|product)\b", 1.5),
    (r"\bwhat\s+(is|are)\s+produced\b", 2.0),
]

_ISSUE_RISK: list[tuple[str, float]] = [
    (r"\brisk[s]?\b", 2.5),
    (r"\bissue[s]?\b", 1.5),
    (r"\buncertaint(y|ies)\b", 2.0),
    (r"\bmitigat(e|ion)\b", 2.0),
    (r"\bcause[s]?\b", 1.0),
    (r"\bimpact[s]?\b", 1.0),
    (r"\bcontrol[s]?\b", 1.0),
    (r"\bproblem[s]?\b", 1.0),
    (r"\bfailure\s+mode[s]?\b", 2.0),
    (r"\bmajor\s+(risk|issue|concern)s?\b", 2.0),
    (r"\b(reduce|manage|address)\s+(risk|uncertainty|issue)", 2.0),
]

_DECISION_TRACE: list[tuple[str, float]] = [
    (r"\bdecision[s]?\b", 2.0),
    (r"\bdecide[s]?\b", 1.5),
    (r"\brationale\b", 2.0),
    (r"\bwhy\s+(was|did|is)\b", 1.5),
    (r"\bbasis\s+for\b", 1.5),
    (r"\bjustif(y|ication|ied)\b", 2.0),
    (r"\bassumption[s]?\s+(underlying|behind|that\s+led)\b", 2.0),
    (r"\bhow\s+was\s+\w+\s+(chosen|decided|selected)\b", 2.5),
    (r"\btrace\s+(the\s+)?decision\b", 3.0),
]

_COMPARISON: list[tuple[str, float]] = [
    (r"\bcompare\b", 2.5),
    (r"\bcompared\s+to\b", 2.0),
    (r"\bvs\b\.?", 2.0),
    (r"\bversus\b", 2.0),
    (r"\bdifference[s]?\s+between\b", 2.5),
    (r"\bsimilarit(y|ies)\s+between\b", 2.0),
    (r"\bcontrast\s+(with|to)\b", 2.0),
    (r"\b(better|worse|stronger|weaker)\s+than\b", 1.5),
]

_COMPLIANCE: list[tuple[str, float]] = [
    (r"\bcompliance\b", 2.5),
    (r"\bcomply\s+with\b", 2.0),
    (r"\bconform\s+to\b", 2.0),
    (r"\bregulation[s]?\b", 1.5),
    (r"\bstandard[s]?\b.*\b(meet|conform|comply)\b", 1.5),
    (r"\baudit\s+(criteria|requirement)", 2.0),
    (r"\bcertification[s]?\b", 1.5),
    (r"\baccredit(ation|ed)\b", 2.0),
]

_LEGAL: list[tuple[str, float]] = [
    (r"\blegal\b", 1.5),
    (r"\bcontract(ual)?\s+(term|terms|provision|clause)\b", 2.5),
    (r"\bnotices?\s+provisions?\b", 2.0),
    (r"\bexecution\s+in\s+counterparts\b", 2.5),
    (r"\bgoverning\s+law\b", 2.5),
    (r"\bindemnif(y|ication)\b", 2.0),
    (r"\bbreach\s+(of\s+)?contract\b", 2.5),
    (r"\bjurisdiction\b", 1.5),
    (r"\bdispute\s+resolution\b", 2.0),
    (r"\bsignatur(y|e)\b.*\b(clause|provision|block)\b", 2.0),
    # Insurance / liability questions surface as legal because
    # the spec collapsed the prior ``insurance_terms`` intent
    # into ``legal_or_contract_terms`` — they share the
    # boilerplate-keep behaviour and the user usually asks
    # about them in one breath ("what are the insurance
    # requirements in this contract?").
    (r"\binsurance\s+(requirements?|coverage|provision)", 3.0),
    (r"\bliabilit(y|ies)\s+(insurance|provision|cap|limit)", 2.5),
    (r"\b(general|professional)\s+liability\b", 2.0),
    (r"\bworkers?\s*compensation\b", 2.0),
]

_COST_EFFORT: list[tuple[str, float]] = [
    (r"\bcost[s]?\b", 1.5),
    (r"\bbudget\b", 2.0),
    (r"\bprice\b", 1.5),
    (r"\bestimate[s]?\b", 1.5),
    (r"\beffort\s+(estimate|required)\b", 2.0),
    (r"\bman-?hours?\b", 2.0),
    (r"\bfee[s]?\b", 1.0),
    (r"\b(how\s+much|how\s+expensive)\b", 2.5),
    (r"\b\$\s*\d+", 1.5),
]

_SCHEDULE: list[tuple[str, float]] = [
    (r"\bschedule\b", 2.0),
    (r"\btimeline\b", 2.0),
    (r"\bdeadline[s]?\b", 2.0),
    (r"\bmilestone[s]?\b", 1.5),
    (r"\bcalendar\b", 1.0),
    (r"\b(start|end|complet(e|ion))\s+date\b", 2.0),
    (r"\bgantt\b", 2.5),
    (r"\bwhen\s+(will|does|is)\b", 1.0),
    (r"\bdurat(ion|ions)\b", 1.5),
    (r"\b\d+\s+(week|month|day|year)s?\b", 0.8),
]


_INTENT_LEXICONS: dict[QueryIntentLabel, list[tuple[str, float]]] = {
    QueryIntentLabel.LIST_EXTRACTION: _LIST_EXTRACTION,
    QueryIntentLabel.SUMMARY_LOOKUP: _SUMMARY_LOOKUP,
    QueryIntentLabel.EXACT_FACT_LOOKUP: _EXACT_FACT_LOOKUP,
    QueryIntentLabel.REQUIREMENTS_LOOKUP: _REQUIREMENTS,
    QueryIntentLabel.RESPONSIBILITY_MAPPING: _RESPONSIBILITY,
    QueryIntentLabel.DEPENDENCY_MAPPING: _DEPENDENCY,
    QueryIntentLabel.STAGE_PROGRESSION: _STAGE_PROGRESSION,
    QueryIntentLabel.DELIVERABLE_MAPPING: _DELIVERABLE,
    QueryIntentLabel.ISSUE_RISK_MAPPING: _ISSUE_RISK,
    QueryIntentLabel.DECISION_TRACE: _DECISION_TRACE,
    QueryIntentLabel.COMPARISON: _COMPARISON,
    QueryIntentLabel.COMPLIANCE_LOOKUP: _COMPLIANCE,
    QueryIntentLabel.LEGAL_OR_CONTRACT_TERMS: _LEGAL,
    QueryIntentLabel.COST_OR_EFFORT_LOOKUP: _COST_EFFORT,
    QueryIntentLabel.SCHEDULE_OR_MILESTONE_LOOKUP: _SCHEDULE,
}


# Precompile once.
_COMPILED: dict[
    QueryIntentLabel, list[tuple[re.Pattern[str], float]]
] = {
    intent: [(re.compile(p, re.IGNORECASE), w) for p, w in lex]
    for intent, lex in _INTENT_LEXICONS.items()
}


# Specificity priority for tie-breaks. More specific shapes win
# when two intents tie — e.g. "list all risks" should be
# ISSUE_RISK_MAPPING, not LIST_EXTRACTION. Adjust here, never by
# inline if/else.
_TIE_BREAK_PRIORITY: list[QueryIntentLabel] = [
    QueryIntentLabel.RESPONSIBILITY_MAPPING,
    QueryIntentLabel.DECISION_TRACE,
    QueryIntentLabel.ISSUE_RISK_MAPPING,
    QueryIntentLabel.DEPENDENCY_MAPPING,
    QueryIntentLabel.STAGE_PROGRESSION,
    QueryIntentLabel.DELIVERABLE_MAPPING,
    QueryIntentLabel.COMPARISON,
    QueryIntentLabel.REQUIREMENTS_LOOKUP,
    QueryIntentLabel.COMPLIANCE_LOOKUP,
    QueryIntentLabel.LEGAL_OR_CONTRACT_TERMS,
    QueryIntentLabel.COST_OR_EFFORT_LOOKUP,
    QueryIntentLabel.SCHEDULE_OR_MILESTONE_LOOKUP,
    QueryIntentLabel.LIST_EXTRACTION,
    QueryIntentLabel.SUMMARY_LOOKUP,
    QueryIntentLabel.EXACT_FACT_LOOKUP,
]


# Minimum total weight for a specific intent to win. Set so one
# strong signal (weight ≥ 1.5) qualifies; weak signals must
# combine to clear the bar.
_GENERIC_THRESHOLD: float = 1.5


@dataclass(frozen=True)
class IntentDetection:
    intent: QueryIntentLabel
    score: float
    scores: dict[QueryIntentLabel, float]
    matched_keywords: dict[QueryIntentLabel, list[str]]

    def signals_payload(self) -> dict[str, object]:
        """Compact projection for the diagnostic event payload."""
        return {
            "winning_score": round(self.score, 2),
            "scores": {
                k.value: round(v, 2)
                for k, v in sorted(
                    self.scores.items(), key=lambda kv: -kv[1],
                )
            },
            # Keep the matched-keyword sample short — the audit
            # log shouldn't grow with very long queries.
            "matched_sample": {
                k.value: v[:5]
                for k, v in self.matched_keywords.items()
            },
        }


def detect_intent(query: str) -> IntentDetection:
    """Score each intent against ``query``; return the winner plus
    the full score map.

    Returns ``GENERIC_LOOKUP`` (score 0) when no intent clears
    the threshold. The score map is still populated when ties /
    sub-threshold matches occur — useful for the audit log."""
    scores: dict[QueryIntentLabel, float] = {}
    matches: dict[QueryIntentLabel, list[str]] = {}
    if not query or not query.strip():
        return IntentDetection(
            intent=QueryIntentLabel.GENERIC_LOOKUP,
            score=0.0,
            scores={},
            matched_keywords={},
        )
    for intent, patterns in _COMPILED.items():
        total = 0.0
        hits: list[str] = []
        for pat, weight in patterns:
            for m in pat.finditer(query):
                total += weight
                hits.append(m.group(0))
        if total > 0:
            scores[intent] = total
            matches[intent] = hits
    if not scores:
        return IntentDetection(
            intent=QueryIntentLabel.GENERIC_LOOKUP,
            score=0.0,
            scores={},
            matched_keywords={},
        )
    # Pick the highest-scoring intent. Ties broken by
    # ``_TIE_BREAK_PRIORITY`` (more-specific shape wins).
    max_score = max(scores.values())
    contenders = [i for i, s in scores.items() if s == max_score]
    if len(contenders) == 1:
        best = contenders[0]
    else:
        priority_map = {
            intent: idx for idx, intent in enumerate(_TIE_BREAK_PRIORITY)
        }
        best = min(contenders, key=lambda i: priority_map.get(i, 999))
    if scores[best] < _GENERIC_THRESHOLD:
        return IntentDetection(
            intent=QueryIntentLabel.GENERIC_LOOKUP,
            score=0.0,
            scores=scores,
            matched_keywords=matches,
        )
    return IntentDetection(
        intent=best,
        score=scores[best],
        scores=scores,
        matched_keywords=matches,
    )


__all__ = [
    "IntentDetection",
    "QueryIntentLabel",
    "detect_intent",
]
