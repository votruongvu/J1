"""Generic domain pack.

The fallback that's always selected when no domain pack scores
above threshold (and no operator override is in play). Carries no
keywords, no overlays, no extraction targets — its job is to give
the registry a stable id (`general`) so consumers don't special-
case 'no domain'.

Generic now ALSO carries cross-domain ``document_profile_rules`` —
RFP / meeting minutes / memo / notes patterns that any deployment
benefits from. Domain-specific packs override these via lower
``priority`` numbers when they want to.
"""

from __future__ import annotations

from j1.domains.models import (
    DocumentProfileRule,
    DocumentProfileRuleHints,
    DomainPack,
)
from j1.domains.registry import DOMAIN_GENERAL


__all__ = ["build_general_pack"]


GENERIC_PROMPT_ADDON = """\
This document does not appear to belong to a specialised domain.
Use the generic ingestion planning rules — prefer fast/balanced
profiles, do not enable expensive domain-specific extractors
without strong evidence."""


# Cross-domain patterns. Priorities sit in the 100+ range so any
# pack with domain-specific rules (priorities ≤ 50) wins. Authoring
# stays in Python — generic rules are tightly coupled to the
# assessment vocabulary so a YAML round-trip would not add value.
_GENERIC_RULES: tuple[DocumentProfileRule, ...] = (
    DocumentProfileRule(
        id="generic_rfp",
        priority=100,
        filename_regex=r"(?i)(\b|_)(rfp|tender|itb|rfq)(\b|_)",
        title_regex=r"(?i)\b(request for proposal|tender|invitation to bid)\b",
        recommended_profile="advanced",
        confidence=0.75,
        reason=(
            "Filename or title suggests an RFP / tender. Likely "
            "carries requirements, tables, and figures — advanced "
            "mode is the safer default."
        ),
        hints=DocumentProfileRuleHints(
            likely_tables=True,
            likely_requirements=True,
            likely_long_document=True,
        ),
    ),
    DocumentProfileRule(
        id="generic_meeting_minutes",
        priority=110,
        filename_regex=r"(?i)(\b|_)(meeting|minutes|mom)(\b|_)",
        title_regex=r"(?i)\b(meeting minutes|minutes of meeting)\b",
        recommended_profile="minimum_queryable",
        confidence=0.7,
        reason=(
            "Filename or title suggests meeting minutes. Usually "
            "short, text-only — minimum queryable is enough."
        ),
    ),
    DocumentProfileRule(
        id="generic_memo",
        priority=120,
        filename_regex=r"(?i)(\b|_)(memo|memorandum)(\b|_)",
        title_regex=r"(?i)\b(memo|memorandum)\b",
        recommended_profile="minimum_queryable",
        confidence=0.7,
        reason=(
            "Filename or title suggests a memo. Usually short, "
            "text-only — minimum queryable is enough."
        ),
    ),
    DocumentProfileRule(
        id="generic_notes",
        priority=130,
        filename_regex=r"(?i)(\b|_)(notes?|jottings?)(\b|_)",
        title_regex=r"(?i)\bnotes\b",
        recommended_profile="minimum_queryable",
        confidence=0.65,
        reason=(
            "Filename or title suggests informal notes. "
            "Minimum queryable is enough."
        ),
    ),
    DocumentProfileRule(
        id="generic_report",
        priority=140,
        filename_regex=r"(?i)(\b|_)(report|whitepaper|study)(\b|_)",
        title_regex=r"(?i)\b(annual report|technical report|whitepaper)\b",
        recommended_profile="standard",
        confidence=0.65,
        reason=(
            "Filename or title suggests a report. Standard mode "
            "balances cost and coverage."
        ),
        hints=DocumentProfileRuleHints(likely_long_document=True),
    ),
)


def build_general_pack() -> DomainPack:
    """Construct the generic fallback pack.

 A no-op detector — the registry skips packs whose `detect=None`,
 so generic never competes with domain packs in auto-detection.
 """
    # Lint Python-authored rules at pack-load time so the YAML and
    # in-code authoring paths share one quality bar.
    from j1.domains.profile_rules import lint_document_profile_rule
    for rule in _GENERIC_RULES:
        lint_document_profile_rule(
            rule.id,
            priority=rule.priority,
            recommended_profile=rule.recommended_profile,
            reason=rule.reason,
            filename_regex=rule.filename_regex,
            title_regex=rule.title_regex,
        )
    return DomainPack(
        id=DOMAIN_GENERAL,
        display_name="Generic",
        version="generic",
        prompt_addon=GENERIC_PROMPT_ADDON,
        detect=None,
        document_profile_rules=_GENERIC_RULES,
    )
