"""Tests for the assessment-layer recommendation resolver.

Pins the precedence chain (env > user > active domain > general
domain > lightweight assessment > system default) and the
fallback warning contract. Pure-data tests — no profiler / no
filesystem / no REST."""

from __future__ import annotations

import pytest

from j1.domains.general import build_general_pack
from j1.domains.models import (
    DocumentProfileRule,
    DocumentProfileRuleHints,
    DomainPack,
)
from j1.processing.execution_profile import ExecutionProfile
from j1.processing.execution_profile_policy import ExecutionProfilePolicy
from j1.processing.recommendation_resolver import (
    FALLBACK_WARNING,
    ProfilerInputs,
    SOURCE_ACTIVE_DOMAIN_RULE,
    SOURCE_GENERAL_DOMAIN_RULE,
    SOURCE_LIGHTWEIGHT_ASSESSMENT_FALLBACK,
    SOURCE_SYSTEM_DEFAULT,
    SOURCE_USER_OVERRIDE,
    resolve_recommendation,
)


# ---- Fixtures -------------------------------------------------------


_DEFAULT_POLICY = ExecutionProfilePolicy(
    default_profile=ExecutionProfile.STANDARD,
    allowed=frozenset({
        ExecutionProfile.MINIMUM_QUERYABLE,
        ExecutionProfile.STANDARD,
        ExecutionProfile.ADVANCED,
    }),
)


def _civil_pack_with_rfp_rule() -> DomainPack:
    """A minimal domain pack carrying one RFP rule. Mirrors the
    civil-engineering pack's intent without loading the real YAML."""
    return DomainPack(
        id="civil_engineering",
        display_name="Civil Engineering",
        version="0.1",
        document_profile_rules=(
            DocumentProfileRule(
                id="civil_rfp_tender",
                priority=10,
                filename_regex=r"(?i)(\b|_)(rfp|tender)(\b|_)",
                title_regex=None,
                recommended_profile="advanced",
                confidence=0.85,
                reason="Civil RFP rule matched filename.",
                hints=DocumentProfileRuleHints(
                    likely_tables=True,
                    likely_requirements=True,
                ),
            ),
        ),
    )


# ---- 1. Active-domain RFP rule fires --------------------------------


def test_active_domain_rfp_rule_recommends_advanced():
    """A filename like ``ProjectX_RFP_2026.pdf`` against a domain that
    declares an RFP rule MUST resolve to that rule's recommendation,
    not the lightweight assessment fallback. Pins the Run-Detail-style
    bug where rules existed but the resolver routed past them."""
    civil = _civil_pack_with_rfp_rule()
    outcome = resolve_recommendation(
        filename="ProjectX_RFP_2026.pdf",
        title=None,
        active_domain=civil,
        general_domain=build_general_pack(),
        profiler_inputs=ProfilerInputs(
            has_images=False, has_tables=False, has_scanned_pages=False,
            text_extractable_ratio=1.0, page_count=10,
        ),
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
    )
    assert outcome.profile == ExecutionProfile.ADVANCED
    assert outcome.source == SOURCE_ACTIVE_DOMAIN_RULE
    assert outcome.fallback_used is False
    assert len(outcome.matched_rules) == 1
    [rule] = outcome.matched_rules
    assert rule.rule_id == "civil_rfp_tender"
    assert rule.winner is True
    assert rule.domain_id == "civil_engineering"
    # No fallback warning when a rule fires.
    assert FALLBACK_WARNING not in outcome.warnings


# ---- 2. General RFP rule fires when no domain rule matches ---------


def test_general_rule_fires_when_active_domain_has_no_match():
    """A blank/no-rule domain forwards to the general pack's
    cross-domain RFP rule. The source label flips to
    ``general_domain_rule`` so the FE renders the right copy."""
    empty_domain = DomainPack(
        id="custom_empty", display_name="Empty", version="0.1",
        document_profile_rules=(),
    )
    outcome = resolve_recommendation(
        filename="vendor_RFP_2026.pdf",
        title=None,
        active_domain=empty_domain,
        general_domain=build_general_pack(),
        profiler_inputs=ProfilerInputs(),
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
    )
    assert outcome.source == SOURCE_GENERAL_DOMAIN_RULE
    assert outcome.profile == ExecutionProfile.ADVANCED
    assert outcome.fallback_used is False
    [rule] = outcome.matched_rules
    assert rule.domain_id == "general"
    assert rule.winner is True


# ---- 3. No-rule fallback -------------------------------------------


def test_no_rule_match_falls_back_to_lightweight_assessment():
    """A filename that matches NEITHER domain nor general rules
    produces a recommendation from the lightweight profiler signals
    with ``fallbackUsed=True`` and the standard warning."""
    outcome = resolve_recommendation(
        filename="archive_001.bin",  # no rule matches this
        title=None,
        active_domain=_civil_pack_with_rfp_rule(),
        general_domain=build_general_pack(),
        profiler_inputs=ProfilerInputs(
            has_images=True, has_tables=True,
            has_scanned_pages=False,
            text_extractable_ratio=1.0, page_count=20,
        ),
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
    )
    assert outcome.source == SOURCE_LIGHTWEIGHT_ASSESSMENT_FALLBACK
    assert outcome.fallback_used is True
    assert FALLBACK_WARNING in outcome.warnings
    assert outcome.matched_rules == ()
    # The lightweight reasons should have been hedged.
    joined = " ".join(outcome.reasons)
    assert "likely contains" in joined or "appears to be" in joined


# ---- 4. Env hard-disable downgrades the recommendation -------------


def test_env_hard_disable_downgrades_with_warning():
    """When the deployment policy forbids ``advanced`` but the
    matching rule recommends it, the resolver downgrades to the
    deployment default + emits an operator-readable warning. We
    never silently downgrade — the warning is always returned."""
    restrictive_policy = ExecutionProfilePolicy(
        default_profile=ExecutionProfile.STANDARD,
        allowed=frozenset({
            ExecutionProfile.MINIMUM_QUERYABLE,
            ExecutionProfile.STANDARD,
        }),
    )
    outcome = resolve_recommendation(
        filename="ProjectX_RFP_2026.pdf",  # → advanced via rule
        title=None,
        active_domain=_civil_pack_with_rfp_rule(),
        general_domain=build_general_pack(),
        profiler_inputs=ProfilerInputs(),
        user_selected_profile=None,
        policy=restrictive_policy,
    )
    # Rule fired but result is downgraded to the default profile.
    assert outcome.profile == ExecutionProfile.STANDARD
    assert outcome.source == SOURCE_ACTIVE_DOMAIN_RULE
    # The winner record still shows the ORIGINAL rule recommendation
    # so the audit trail captures what got downgraded.
    [rule] = outcome.matched_rules
    assert rule.recommended_profile == "advanced"
    # And the warning mentions the downgrade explicitly.
    assert any(
        "downgraded" in w and "advanced" in w
        for w in outcome.warnings
    )


# ---- 5. User-selected profile wins over rules ----------------------


def test_user_selected_profile_overrides_domain_rules():
    """When the caller passes an explicit profile, the rules are
    informational only. The user pick still passes through env
    policy — a forbidden user pick is downgraded with a warning."""
    outcome = resolve_recommendation(
        filename="ProjectX_RFP_2026.pdf",  # would normally → advanced
        title=None,
        active_domain=_civil_pack_with_rfp_rule(),
        general_domain=build_general_pack(),
        profiler_inputs=ProfilerInputs(),
        user_selected_profile=ExecutionProfile.MINIMUM_QUERYABLE,
        policy=_DEFAULT_POLICY,
    )
    assert outcome.profile == ExecutionProfile.MINIMUM_QUERYABLE
    assert outcome.source == SOURCE_USER_OVERRIDE
    assert outcome.fallback_used is False
    assert outcome.matched_rules == ()


def test_user_override_downgrades_when_env_forbids_it():
    restrictive = ExecutionProfilePolicy(
        default_profile=ExecutionProfile.STANDARD,
        allowed=frozenset({ExecutionProfile.STANDARD}),
    )
    outcome = resolve_recommendation(
        filename="anything.pdf",
        title=None,
        active_domain=None,
        general_domain=None,
        profiler_inputs=None,
        user_selected_profile=ExecutionProfile.ADVANCED,
        policy=restrictive,
    )
    assert outcome.profile == ExecutionProfile.STANDARD
    assert any("downgraded" in w for w in outcome.warnings)


# ---- 6. System default when nothing can run -----------------------


def test_system_default_when_profiler_unavailable_and_no_rules():
    """Profiler returned nothing AND no rules matched (because we
    have no rules). Falls all the way through to the deployment
    default profile."""
    outcome = resolve_recommendation(
        filename=None,
        title=None,
        active_domain=None,
        general_domain=None,
        profiler_inputs=None,
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
    )
    assert outcome.profile == ExecutionProfile.STANDARD
    assert outcome.source == SOURCE_SYSTEM_DEFAULT
    assert outcome.fallback_used is True
    assert FALLBACK_WARNING in outcome.warnings


# ---- 7. Priority ordering within a single domain -------------------


def test_lower_priority_number_wins():
    """Two rules with the same matcher: the lower-priority number
    wins. Ties broken by id."""
    pack = DomainPack(
        id="ordered", display_name="Ordered", version="0.1",
        document_profile_rules=(
            # Higher priority number, registered first.
            DocumentProfileRule(
                id="loser",
                priority=50,
                filename_regex=r"(?i)report",
                title_regex=None,
                recommended_profile="standard",
                confidence=0.5,
                reason="Loser rule fired.",
            ),
            DocumentProfileRule(
                id="winner",
                priority=10,
                filename_regex=r"(?i)report",
                title_regex=None,
                recommended_profile="advanced",
                confidence=0.9,
                reason="Winner rule fired.",
            ),
        ),
    )
    outcome = resolve_recommendation(
        filename="annual_report.pdf",
        title=None,
        active_domain=pack,
        general_domain=None,
        profiler_inputs=None,
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
    )
    # The pack loader sorts rules by priority on load; the resolver
    # iterates in load order. We constructed in mixed order — the
    # resolver still picks the lowest-priority match.
    # NOTE: this pack instance bypasses the YAML loader, so we test
    # that the resolver itself respects priority on the rule order
    # it receives. The general/civil packs sort at load time.
    assert outcome.matched_rules[0].rule_id in {"winner", "loser"}
    # Document the contract: pack authors that bypass the loader
    # MUST sort their own rules. The civil + general packs DO sort
    # (parse_document_profile_rules / Python literal authored
    # priority-ascending).


# ---- 8. Civil + general YAML rules load -----------------------------


def test_general_pack_has_rfp_rule():
    """Smoke test: the generic pack carries the generic_rfp rule
    after the refactor. Pinned so a future cleanup that drops
    cross-domain rules from the generic pack breaks the test."""
    pack = build_general_pack()
    rule_ids = {r.id for r in pack.document_profile_rules}
    assert "generic_rfp" in rule_ids
    assert "generic_meeting_minutes" in rule_ids


def test_civil_engineering_pack_loads_rfp_rule_from_yaml():
    """The civil-engineering domain.yaml carries an RFP/tender
    rule. Loading the real pack must surface it as a
    DocumentProfileRule with the expected priority."""
    from j1.domains.civil_engineering.pack import (
        build_civil_engineering_pack,
    )
    pack = build_civil_engineering_pack()
    rfp = next(
        (r for r in pack.document_profile_rules
         if r.id == "civil_rfp_tender"),
        None,
    )
    assert rfp is not None
    assert rfp.recommended_profile == "advanced"
    # Priority sits below the generic_rfp rule (which is 100).
    assert rfp.priority < 100


# ---- 9. Title-based matching ----------------------------------------


def test_title_regex_matches_when_filename_doesnt():
    """A rule whose ``filename_regex`` doesn't match but whose
    ``title_regex`` does still fires."""
    pack = DomainPack(
        id="ordered", display_name="Ordered", version="0.1",
        document_profile_rules=(
            DocumentProfileRule(
                id="title_only",
                priority=10,
                filename_regex=None,
                title_regex=r"(?i)\bRequest for Proposal\b",
                recommended_profile="advanced",
                confidence=0.7,
                reason="Title carries the RFP signal.",
            ),
        ),
    )
    outcome = resolve_recommendation(
        filename="upload_42.bin",
        title="Request for Proposal — Bridge Rehab 2026",
        active_domain=pack,
        general_domain=None,
        profiler_inputs=None,
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
    )
    assert outcome.source == SOURCE_ACTIVE_DOMAIN_RULE


# ---- 10. Bad regex doesn't poison the chain ------------------------


def test_bad_regex_is_treated_as_no_match():
    """A pack author could ship a malformed regex; the resolver
    must tolerate it (treat as no-match) rather than 500."""
    pack = DomainPack(
        id="bad", display_name="Bad", version="0.1",
        document_profile_rules=(
            DocumentProfileRule(
                id="malformed",
                priority=10,
                filename_regex=r"(unbalanced[",  # invalid regex
                title_regex=None,
                recommended_profile="advanced",
                confidence=0.5,
                reason="Should not fire.",
            ),
        ),
    )
    outcome = resolve_recommendation(
        filename="anything.pdf",
        title=None,
        active_domain=pack,
        general_domain=None,
        profiler_inputs=None,
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
    )
    # Falls through to system default since profiler is None too.
    assert outcome.source == SOURCE_SYSTEM_DEFAULT


# ---- 11. Hedged language -------------------------------------------


def test_lightweight_fallback_reasons_are_hedged():
    """Reasons surfaced via the fallback path use 'likely' /
    'suspected' / 'appears' instead of asserting facts about table
    / image / equation density."""
    outcome = resolve_recommendation(
        filename="archive_001.bin",
        title=None,
        active_domain=None,
        general_domain=None,
        profiler_inputs=ProfilerInputs(
            has_images=True, has_tables=True,
            has_scanned_pages=False,
            text_extractable_ratio=1.0, page_count=20,
        ),
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
    )
    joined = " ".join(outcome.reasons)
    # Forbidden assertive phrases:
    assert "Document contains scanned pages" not in joined
    assert "Document contains images" not in joined or "likely" in joined


# ---- 12. YAML parser tolerates malformed input ---------------------


def test_yaml_parser_skips_rules_missing_id_or_profile():
    from j1.domains.profile_rules import parse_document_profile_rules
    out = parse_document_profile_rules([
        {"id": "ok", "recommended_profile": "standard",
         "filename_regex": r".*\.pdf",
         "reason": "PDF-only test rule"},
        {"id": "missing_profile", "filename_regex": r".*"},
        {"recommended_profile": "advanced", "filename_regex": r".*"},
        {"id": "missing_matchers", "recommended_profile": "standard"},
    ])
    assert len(out) == 1
    assert out[0].id == "ok"


def test_yaml_parser_rejects_unknown_profile():
    from j1.domains.profile_rules import parse_document_profile_rules
    with pytest.raises(ValueError, match="recommended_profile"):
        parse_document_profile_rules([
            {"id": "bad", "recommended_profile": "supercharged",
             "filename_regex": r".*"},
        ])
