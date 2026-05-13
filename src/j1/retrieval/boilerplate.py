"""Generic boilerplate detector + intent-aware demotion.

The audit symptom: irrelevant insurance-requirement / agreement-
signature / exhibit-template chunks pushed out relevant analytical
content because the reranker scored them on lexical overlap alone
— and contract templates repeat keywords like "risk",
"responsibility", "task" by sheer template repetition.

Fix: score *demotion* (not hard filter) tied to intent. Boilerplate
stays retrievable when the user actually asks for legal /
compliance / contract content; otherwise it gets buried under
analytical chunks.

Categories matched (generic across all contract / proposal /
solicitation / template documents — not specific to any one
customer or domain):

  - insurance_requirements
  - agreement_signature
  - notices_provision
  - execution_in_counterparts
  - standard_terms (entire-agreement / severability / etc.)
  - administrative_instructions (proposal format / evaluation
    criteria template)
  - signature_block (in-witness-whereof / signed-and-sealed)
  - generic_exhibit_template

The patterns target section headings and the first ~160 chars
where the template phrasing repeats. The detector returns
``None`` for everything else; the demote function returns a
multiplier the reranker applies to the candidate's final score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from j1.retrieval.intent_router import QueryIntentLabel


class BoilerplateCategory(StrEnum):
    INSURANCE_REQUIREMENTS = "insurance_requirements"
    AGREEMENT_SIGNATURE = "agreement_signature"
    NOTICES_PROVISION = "notices_provision"
    EXECUTION_COUNTERPARTS = "execution_in_counterparts"
    STANDARD_TERMS = "standard_terms"
    ADMINISTRATIVE_INSTRUCTIONS = "administrative_instructions"
    SIGNATURE_BLOCK = "signature_block"
    GENERIC_EXHIBIT_TEMPLATE = "generic_exhibit_template"


_PATTERNS: list[tuple[BoilerplateCategory, re.Pattern[str]]] = [
    (
        BoilerplateCategory.INSURANCE_REQUIREMENTS,
        re.compile(
            r"\b(insurance\s+(requirements?|coverage|certificates?)"
            r"|certificate[s]?\s+of\s+insurance"
            r"|workers?\s*compensation\s+insurance"
            r"|professional\s+liability\s+insurance"
            r"|commercial\s+general\s+liability"
            r"|errors\s+and\s+omissions\s+insurance)\b",
            re.IGNORECASE,
        ),
    ),
    (
        BoilerplateCategory.SIGNATURE_BLOCK,
        re.compile(
            r"\b(in\s+witness\s+whereof"
            r"|signed\s+and\s+sealed"
            r"|authorized\s+representative\s+signature"
            r"|signature\s+page\b)",
            re.IGNORECASE,
        ),
    ),
    (
        BoilerplateCategory.AGREEMENT_SIGNATURE,
        re.compile(
            r"\b(signature\s+block"
            r"|signed\s+as\s+of\s+the\s+date\s+first\s+written)\b",
            re.IGNORECASE,
        ),
    ),
    (
        BoilerplateCategory.NOTICES_PROVISION,
        re.compile(
            r"\b(notices?\s+shall\s+be\s+(in\s+writing|delivered)"
            r"|all\s+notices\s+(under\s+this|required\s+by)"
            r"|notices?\s+to\s+the\s+parties)\b",
            re.IGNORECASE,
        ),
    ),
    (
        BoilerplateCategory.EXECUTION_COUNTERPARTS,
        re.compile(
            r"\b(executed\s+in\s+counterparts"
            r"|may\s+be\s+executed\s+in\s+any\s+number\s+of\s+counterparts"
            r"|counterparts.*facsimile)\b",
            re.IGNORECASE,
        ),
    ),
    (
        BoilerplateCategory.STANDARD_TERMS,
        re.compile(
            r"\b(entire\s+agreement"
            r"|severabilit(y|ies)"
            r"|miscellaneous\s+provisions"
            r"|general\s+conditions\b"
            r"|standard\s+(terms|consulting\s+agreement\s+terms))\b",
            re.IGNORECASE,
        ),
    ),
    (
        BoilerplateCategory.ADMINISTRATIVE_INSTRUCTIONS,
        re.compile(
            r"\b(proposal\s+format\s+requirements?"
            r"|submittal\s+instructions"
            r"|evaluation\s+criteria\s+(for|will\s+be)"
            r"|how\s+to\s+submit\s+(your\s+)?proposal"
            r"|format\s+and\s+content\s+of\s+the\s+proposal)\b",
            re.IGNORECASE,
        ),
    ),
    (
        BoilerplateCategory.GENERIC_EXHIBIT_TEMPLATE,
        re.compile(
            r"\b(exhibit\s+[A-Z]\b\s*[-—:]\s*(template|sample|form|standard)"
            r"|appendix\s+[A-Z]\b\s*[-—:]\s*(template|sample|form|standard))\b",
            re.IGNORECASE,
        ),
    ),
]


@dataclass(frozen=True)
class BoilerplateMatch:
    category: BoilerplateCategory
    matched_text: str


def is_boilerplate_chunk(
    *,
    section_path: str | None = None,
    heading: str | None = None,
    body_preview: str | None = None,
) -> BoilerplateMatch | None:
    """Return the matched category for a chunk's metadata + body
    preview, or ``None`` when no pattern fires.

    Caller provides the SHORT signal set we trust:
      * ``section_path``  — heading-hierarchy join from the
                            parser, e.g. "Exhibit B / Insurance".
      * ``heading``       — the chunk's own heading.
      * ``body_preview``  — first ~160 chars of the chunk body.

    We intentionally don't scan the full body — boilerplate
    headers are short and recur in fixed positions; scanning the
    whole body would produce false positives when a normal task
    description mentions "insurance" once."""
    parts: list[str] = []
    if section_path:
        parts.append(section_path)
    if heading:
        parts.append(heading)
    if body_preview:
        parts.append(body_preview[:160])
    if not parts:
        return None
    haystack = " | ".join(parts)
    for category, pattern in _PATTERNS:
        m = pattern.search(haystack)
        if m:
            return BoilerplateMatch(
                category=category, matched_text=m.group(0),
            )
    return None


# ---- Intent-aware demotion ---------------------------------------
#
# Multipliers in [0, 1] applied to the candidate's rerank score.
# 1.0 = no demotion (intent legitimately wants this category).
# Smaller values aggressively bury the chunk.

# Intents that legitimately want boilerplate content.
# When the active intent is in this set, demotion is bypassed —
# the boilerplate matcher still RUNS (so the audit log records
# the category) but the reranker sees the unmodified score.
_EXEMPT_INTENTS: frozenset[str] = frozenset({
    "legal_or_contract_terms",
    "compliance_lookup",
    # Administrative content is what the user asks for when they
    # ask "how do I submit a proposal" — exempt that intent too
    # via a generic name when one is added.
})


_DEMOTION_BY_CATEGORY: dict[BoilerplateCategory, float] = {
    BoilerplateCategory.INSURANCE_REQUIREMENTS: 0.05,
    BoilerplateCategory.AGREEMENT_SIGNATURE: 0.05,
    BoilerplateCategory.SIGNATURE_BLOCK: 0.05,
    BoilerplateCategory.EXECUTION_COUNTERPARTS: 0.05,
    BoilerplateCategory.NOTICES_PROVISION: 0.10,
    BoilerplateCategory.STANDARD_TERMS: 0.10,
    BoilerplateCategory.ADMINISTRATIVE_INSTRUCTIONS: 0.20,
    BoilerplateCategory.GENERIC_EXHIBIT_TEMPLATE: 0.20,
}


def boilerplate_demotion(
    category: BoilerplateCategory,
    intent: "QueryIntentLabel | str | None",
) -> float:
    """Return the score multiplier for ``category`` under ``intent``.

    No active intent → conservative default (no full demotion).
    Active intent in ``_EXEMPT_INTENTS`` → 1.0 (no demotion).
    Otherwise the per-category multiplier."""
    if intent is None:
        return _DEMOTION_BY_CATEGORY.get(category, 0.30)
    intent_str = intent.value if hasattr(intent, "value") else str(intent)
    if intent_str in _EXEMPT_INTENTS:
        return 1.0
    return _DEMOTION_BY_CATEGORY.get(category, 0.30)


__all__ = [
    "BoilerplateCategory",
    "BoilerplateMatch",
    "boilerplate_demotion",
    "is_boilerplate_chunk",
]
