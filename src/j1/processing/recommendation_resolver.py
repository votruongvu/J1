"""Assessment-layer recommendation resolver.

Applies the precedence chain that turns "the user uploaded a file"
into "this is the recommended execution profile, and here's WHY,
and here are the warnings the FE should surface".

Precedence (highest authority first):

    1. env hard-disable     — deployment policy forbids the
                              candidate profile; downgrade to the
                              nearest allowed profile and warn.
    2. user-selected        — caller passed an explicit profile
                              (e.g. operator overriding the
                              recommendation). Still subject to (1).
    3. active-domain rules  — the selected domain pack's
                              ``document_profile_rules``. First
                              match by ascending ``priority``.
    4. general-domain rules — the ``general`` pack's rules; used
                              when the active domain isn't general
                              AND no active-domain rule matched.
    5. lightweight signals  — the deterministic profiler's
                              recommendation
                              (``recommend_profile_from_assessment``).
                              Sets ``fallback_used=True`` and emits
                              the "no domain rule matched" warning.
    6. system default       — when even the lightweight path can't
                              produce a recommendation (e.g.
                              profiler returned nothing), use the
                              deployment's default profile.

The resolver does NOT call MinerU, RAGAnything, an LLM, or
PyMuPDF. It's pure data dispatch + regex match. All authority
above the FE picker (deployment env, allow-list) is bounded by
:class:`ExecutionProfilePolicy`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from j1.domains.models import (
    DocumentProfileRule,
    DocumentProfileRuleHints,
    DomainPack,
)
from j1.processing.execution_profile import (
    ExecutionProfile,
    recommend_profile_from_assessment,
)
from j1.processing.execution_profile_policy import (
    ExecutionProfilePolicy,
    ProfileNotAllowedError,
)


__all__ = [
    "RecommendationOutcome",
    "MatchedRuleRecord",
    "ProfilerInputs",
    "RECOMMENDATION_SOURCES",
    "resolve_recommendation",
]


# Wire vocabulary — pinned by tests + surfaced to the FE so a
# dialog can render the right copy without parsing free-form text.
SOURCE_USER_OVERRIDE = "user_override"
SOURCE_ACTIVE_DOMAIN_RULE = "active_domain_rule"
SOURCE_GENERAL_DOMAIN_RULE = "general_domain_rule"
SOURCE_LIGHTWEIGHT_ASSESSMENT = "lightweight_assessment"
SOURCE_LIGHTWEIGHT_ASSESSMENT_FALLBACK = "lightweight_assessment_fallback"
SOURCE_SYSTEM_DEFAULT = "system_default"

RECOMMENDATION_SOURCES = (
    SOURCE_USER_OVERRIDE,
    SOURCE_ACTIVE_DOMAIN_RULE,
    SOURCE_GENERAL_DOMAIN_RULE,
    SOURCE_LIGHTWEIGHT_ASSESSMENT,
    SOURCE_LIGHTWEIGHT_ASSESSMENT_FALLBACK,
    SOURCE_SYSTEM_DEFAULT,
)


# Standard fallback warning text. Pinned in the FE test so a wording
# drift on either side is caught.
FALLBACK_WARNING = (
    "No domain-specific document rule matched this filename/title. "
    "This recommendation is based on lightweight assessment only. "
    "Please choose based on the visible complexity of the document."
)


@dataclass(frozen=True)
class MatchedRuleRecord:
    """One rule that fired during resolution.

    The resolver collects all matching rules (not just the winner)
    so the FE audit panel can show why a candidate was demoted by
    priority. ``domain_id`` is the pack the rule came from. The
    ``winner`` flag tells consumers which one drove the final
    recommendation.
    """

    rule_id: str
    domain_id: str
    priority: int
    recommended_profile: str
    confidence: float
    reason: str
    hints: DocumentProfileRuleHints = field(
        default_factory=DocumentProfileRuleHints,
    )
    winner: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "domainId": self.domain_id,
            "priority": self.priority,
            "recommendedProfile": self.recommended_profile,
            "confidence": self.confidence,
            "reason": self.reason,
            "winner": self.winner,
            "hints": {
                "likelyTables": self.hints.likely_tables,
                "likelyImages": self.hints.likely_images,
                "likelyRequirements": self.hints.likely_requirements,
                "likelyScanned": self.hints.likely_scanned,
                "likelyLongDocument": self.hints.likely_long_document,
            },
        }


@dataclass(frozen=True)
class ProfilerInputs:
    """Subset of ``DocumentProfile`` that the lightweight path uses.

    Decoupled from the full profiler dataclass so the resolver is
    unit-testable without spinning up the PDF profiler. The REST
    handler builds this from the real ``DocumentProfile`` instance.
    """

    has_images: bool = False
    has_tables: bool = False
    has_scanned_pages: bool = False
    text_extractable_ratio: float = 1.0
    page_count: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecommendationOutcome:
    """Resolver output. The REST handler serialises this onto the
    AssessmentPlanResponse envelope; the FE renders ``source`` and
    ``fallback_used`` next to the picker."""

    profile: ExecutionProfile
    source: str
    fallback_used: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    matched_rules: tuple[MatchedRuleRecord, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "recommendedProfile": self.profile.value,
            "recommendationSource": self.source,
            "fallbackUsed": self.fallback_used,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "matchedRules": [r.to_payload() for r in self.matched_rules],
        }


# ---------------------------------------------------------------- API


def resolve_recommendation(
    *,
    filename: str | None,
    title: str | None,
    active_domain: DomainPack | None,
    general_domain: DomainPack | None,
    profiler_inputs: ProfilerInputs | None,
    user_selected_profile: ExecutionProfile | str | None,
    policy: ExecutionProfilePolicy,
) -> RecommendationOutcome:
    """Run the full precedence chain. Pure function; no I/O.

    Arguments:
      filename / title   — the matchers ``document_profile_rules``
                           consume. Either may be empty; rules that
                           require a missing matcher just don't fire.
      active_domain      — the user/workspace-selected or auto-
                           detected domain pack. May be ``None`` or
                           the general pack itself; either is fine.
      general_domain     — the canonical ``general`` pack. Optional
                           so legacy/test wirings don't have to wire
                           it just to call the resolver.
      profiler_inputs    — lightweight signals from
                           ``DeterministicDocumentProfiler``. ``None``
                           means the profiler couldn't analyse the
                           file; the resolver still produces an
                           outcome (system_default).
      user_selected_profile
                         — explicit operator pick. ``None`` means
                           "let the system recommend".
      policy             — deployment env policy. Drives the final
                           env-disable check.
    """
    warnings: list[str] = []
    reasons: list[str] = []
    matched_rules: list[MatchedRuleRecord] = []

    # Always surface profiler warnings so the FE never silently
    # drops file-size / parse hints.
    if profiler_inputs is not None:
        warnings.extend(profiler_inputs.warnings)

    # ---- 1. user override (still subject to env policy) ----
    if user_selected_profile is not None:
        chosen = _coerce_profile(user_selected_profile)
        reasons.append(
            f"User explicitly selected {chosen.value!r}; the "
            "system recommendation is informational only."
        )
        downgraded, downgrade_warning = _apply_env_policy(chosen, policy)
        if downgrade_warning:
            warnings.append(downgrade_warning)
        return RecommendationOutcome(
            profile=downgraded,
            source=SOURCE_USER_OVERRIDE,
            fallback_used=False,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            matched_rules=(),
        )

    # ---- 2. active-domain rules ----
    active_match: MatchedRuleRecord | None = None
    if active_domain is not None and active_domain.document_profile_rules:
        active_match = _first_matching_rule(
            filename=filename, title=title,
            rules=active_domain.document_profile_rules,
            domain_id=active_domain.id,
        )
        if active_match is not None:
            matched_rules.append(active_match)

    general_match: MatchedRuleRecord | None = None
    if (
        active_match is None
        and general_domain is not None
        and general_domain.document_profile_rules
        # Avoid double-counting when active_domain IS the general pack.
        and (
            active_domain is None
            or active_domain.id != general_domain.id
        )
    ):
        general_match = _first_matching_rule(
            filename=filename, title=title,
            rules=general_domain.document_profile_rules,
            domain_id=general_domain.id,
        )
        if general_match is not None:
            matched_rules.append(general_match)

    winner = active_match or general_match
    if winner is not None:
        chosen = _coerce_profile(winner.recommended_profile)
        winner_record = MatchedRuleRecord(
            rule_id=winner.rule_id,
            domain_id=winner.domain_id,
            priority=winner.priority,
            recommended_profile=winner.recommended_profile,
            confidence=winner.confidence,
            reason=winner.reason,
            hints=winner.hints,
            winner=True,
        )
        matched_rules = [winner_record]
        reasons.append(winner.reason)
        downgraded, downgrade_warning = _apply_env_policy(chosen, policy)
        if downgrade_warning:
            warnings.append(downgrade_warning)
        source = (
            SOURCE_ACTIVE_DOMAIN_RULE
            if winner is active_match
            else SOURCE_GENERAL_DOMAIN_RULE
        )
        return RecommendationOutcome(
            profile=downgraded,
            source=source,
            fallback_used=False,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            matched_rules=tuple(matched_rules),
        )

    # ---- 3. lightweight assessment fallback ----
    if profiler_inputs is not None:
        recommended, lightweight_reasons = recommend_profile_from_assessment(
            has_images=profiler_inputs.has_images,
            has_tables=profiler_inputs.has_tables,
            has_scanned_pages=profiler_inputs.has_scanned_pages,
            text_extractable_ratio=profiler_inputs.text_extractable_ratio,
            page_count=profiler_inputs.page_count,
        )
        reasons.extend(_hedge_reasons(lightweight_reasons))
        warnings.append(FALLBACK_WARNING)
        downgraded, downgrade_warning = _apply_env_policy(
            recommended, policy,
        )
        if downgrade_warning:
            warnings.append(downgrade_warning)
        return RecommendationOutcome(
            profile=downgraded,
            source=SOURCE_LIGHTWEIGHT_ASSESSMENT_FALLBACK,
            fallback_used=True,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            matched_rules=(),
        )

    # ---- 4. system default ----
    reasons.append(
        "Document signals unavailable; falling back to the "
        "deployment default profile."
    )
    warnings.append(FALLBACK_WARNING)
    return RecommendationOutcome(
        profile=policy.default_profile,
        source=SOURCE_SYSTEM_DEFAULT,
        fallback_used=True,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        matched_rules=(),
    )


# ---------------------------------------------------------------- impl


def _first_matching_rule(
    *,
    filename: str | None,
    title: str | None,
    rules: tuple[DocumentProfileRule, ...],
    domain_id: str,
) -> MatchedRuleRecord | None:
    """Return the first rule (by ascending priority) that matches.

    The pack loader sorts rules by priority at load time so this
    function can do a single pass.
    """
    fname = filename or ""
    ttl = title or ""
    for rule in rules:
        if not _rule_matches(rule, fname, ttl):
            continue
        return MatchedRuleRecord(
            rule_id=rule.id,
            domain_id=domain_id,
            priority=rule.priority,
            recommended_profile=rule.recommended_profile,
            confidence=rule.confidence,
            reason=rule.reason,
            hints=rule.hints,
            winner=False,
        )
    return None


def _rule_matches(
    rule: DocumentProfileRule, filename: str, title: str,
) -> bool:
    """A rule matches when ANY of its declared patterns matches the
    corresponding input. A bad regex is treated as "doesn't match"
    rather than raising — operators can't be expected to ship a
    pack with perfect regex and we don't want one broken rule to
    poison the rest of the chain."""
    if rule.filename_regex:
        try:
            if filename and re.search(rule.filename_regex, filename):
                return True
        except re.error:
            pass
    if rule.title_regex:
        try:
            if title and re.search(rule.title_regex, title):
                return True
        except re.error:
            pass
    return False


def _apply_env_policy(
    profile: ExecutionProfile,
    policy: ExecutionProfilePolicy,
) -> tuple[ExecutionProfile, str | None]:
    """If the candidate isn't on the allow-list, downgrade to the
    deployment default + emit an operator-readable warning. We
    never silently downgrade; the warning is always returned to
    the FE alongside the chosen profile.

    Distinct from ``policy.resolve()`` which raises 403 — the
    assessment-layer recommendation is advisory, not an enforcement
    point. The actual ingest endpoint still rejects forbidden
    explicit picks with the policy's normal error.
    """
    if policy.is_allowed(profile):
        return profile, None
    fallback = policy.default_profile
    warning = (
        f"Deployment policy disables {profile.value!r}; recommendation "
        f"downgraded to {fallback.value!r}. Allowed profiles: "
        f"{', '.join(sorted(p.value for p in policy.allowed))}."
    )
    return fallback, warning


def _coerce_profile(value: ExecutionProfile | str) -> ExecutionProfile:
    if isinstance(value, ExecutionProfile):
        return value
    try:
        return ExecutionProfile(str(value).strip())
    except ValueError as exc:
        raise ProfileNotAllowedError(
            requested=ExecutionProfile.STANDARD,
            allowed=frozenset(),
        ) from exc


_HEDGE_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    # Existing recommend_profile_from_assessment phrases that claim
    # certainty we don't actually have. Soften them so the FE reads
    # as "suspected / likely" rather than "we know there are tables".
    ("Document contains scanned pages", "Document likely contains scanned pages"),
    ("Document contains images", "Document likely contains images"),
    ("Document contains tables", "Document likely contains tables"),
    ("Document contains images and tables", "Document likely contains images and tables"),
    ("Document is text-only", "Document appears to be mostly text"),
    ("Very little text could be extracted directly", "Very little text could be extracted directly (suspected scanned content)"),
)


def _hedge_reasons(reasons: tuple[str, ...]) -> list[str]:
    """Substitute claim-y phrases with hedged equivalents. Same
    contract as the FE: 'suspected' / 'likely' / 'appears' instead
    of asserting fact about table / image / equation density."""
    out: list[str] = []
    for r in reasons:
        hedged = r
        for needle, replacement in _HEDGE_SUBSTITUTIONS:
            if needle in hedged:
                hedged = hedged.replace(needle, replacement)
        out.append(hedged)
    return out
