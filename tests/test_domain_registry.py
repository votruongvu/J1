"""Tests for the domain registry + selection precedence.

Pins the spec's selection rules: user → workspace → auto-detect →
fallback to general."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from j1.domains import (
    DOMAIN_GENERAL,
    DomainContext,
    DomainDetectionResult,
    DomainPack,
    DomainRegistry,
    default_registry,
)
from j1.domains.registry import (
    DOMAIN_SELECTION_AUTO_DETECTED,
    DOMAIN_SELECTION_FALLBACK_GENERAL,
    DOMAIN_SELECTION_USER,
    DOMAIN_SELECTION_WORKSPACE,
    select_domain,
)


@dataclass
class _Ctx:
    """Minimal detection context — populates only the fields the
 civil pack actually reads."""

    title: str = ""
    title_quality: str = "clear"
    filename: str | None = None
    early_page_text: str = ""
    heading_outline: tuple = ()
    table_captions: tuple = ()
    image_captions: tuple = ()
    document_type_hint: str | None = None
    table_header_rows: tuple = ()


# ---- Default registry -------------------------------------------


def test_default_registry_contains_general_and_civil_engineering():
    reg = default_registry()
    assert "general" in reg.list_ids()
    assert "civil_engineering" in reg.list_ids()


def test_general_pack_has_no_detector():
    """`general` exists for fallback and never competes in
 auto-detection — its `detect` callable is None."""
    reg = default_registry()
    assert reg.get("general").detect is None


def test_extended_document_types_includes_civil_types():
    reg = default_registry()
    extended = reg.extended_document_types()
    assert "boq" in extended
    assert "inspection_report" in extended
    assert "method_statement" in extended


# ---- Selection precedence ---------------------------------------


def test_unknown_user_override_falls_back_with_warning():
    """Bad domain id → `general` + warning so reviewers see the
 misconfiguration."""
    ctx = _Ctx(title="Whatever")
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        user_override="space_engineering",
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == DOMAIN_GENERAL
    assert any("unknown domain" in w for w in result.warnings)


def test_user_override_civil_selects_civil_even_for_generic_text():
    """Operator forced civil — honored even when evidence is weak.
 A warning is added so reviewers see the mismatch."""
    ctx = _Ctx(title="Quarterly Business Review")
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        user_override="civil_engineering",
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == "civil_engineering"
    assert result.selection_source == DOMAIN_SELECTION_USER
    assert any("override" in w.lower() for w in result.warnings)


def test_user_override_blocked_when_not_in_allow_list():
    ctx = _Ctx(title="BOQ - drainage")
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        user_override="civil_engineering",
        allowed_overrides=frozenset({"general"}),
    )
    assert result.selected_domain == DOMAIN_GENERAL
    assert any("allow-list" in w for w in result.warnings)


def test_workspace_default_selects_civil():
    ctx = _Ctx(title="generic site memo")
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        workspace_default="civil_engineering",
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == "civil_engineering"
    assert result.selection_source == DOMAIN_SELECTION_WORKSPACE


def test_user_override_wins_over_workspace_default():
    ctx = _Ctx(title="generic")
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        user_override="general",
        workspace_default="civil_engineering",
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == DOMAIN_GENERAL


def test_auto_detect_boq_selects_civil_engineering():
    ctx = _Ctx(
        title="Bill of Quantities — Road Drainage Works",
        filename="BOQ_drainage.pdf",
        early_page_text="excavation, culvert, concrete, backfill",
        table_header_rows=(("Item", "Description", "Unit", "Quantity", "Rate", "Amount"),),
    )
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == "civil_engineering"
    assert result.selection_source == DOMAIN_SELECTION_AUTO_DETECTED
    assert result.confidence >= 0.65


def test_auto_detect_inspection_report():
    ctx = _Ctx(
        title="Site Inspection Report — Basement Slab Cracks",
        filename="inspection.pdf",
        early_page_text="concrete defects, contractor, recommendation",
    )
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == "civil_engineering"
    assert "civil_engineering.detect.inspection_report" in result.applied_domain_rules


def test_generic_business_report_falls_back_to_general():
    ctx = _Ctx(
        title="Quarterly Business Review",
        filename="qbr.pdf",
        early_page_text="sales pipeline revenue customers churn",
    )
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == DOMAIN_GENERAL
    assert result.selection_source == DOMAIN_SELECTION_FALLBACK_GENERAL


def test_weak_civil_signals_below_threshold_fall_back():
    """A document that mentions 'site' and 'project' once is not
 enough to push past the default threshold."""
    ctx = _Ctx(
        title="Weekly Status Update",
        filename="weekly.pdf",
        early_page_text=(
            "Site visit was uneventful. Project is on track."
        ),
    )
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        detection_threshold=0.65,
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == DOMAIN_GENERAL
    # The fallback context still surfaces the candidate evidence.
    assert any(c.domain_id == "civil_engineering" for c in result.candidates) \
        or len(result.candidates) == 0


def test_detection_disabled_falls_back_to_general():
    ctx = _Ctx(
        title="Bill of Quantities — Road Drainage Works",
        early_page_text="excavation",
    )
    result = select_domain(
        registry=default_registry(),
        detection_context=ctx,
        detection_enabled=False,
        allowed_overrides=frozenset({"general", "civil_engineering"}),
    )
    assert result.selected_domain == DOMAIN_GENERAL


# ---- Custom registry --------------------------------------------


def test_custom_registry_with_only_general_pack_always_falls_back():
    reg = DomainRegistry()
    from j1.domains.general import build_general_pack
    reg.register(build_general_pack())
    ctx = _Ctx(title="Bill of Quantities — Road Drainage")
    result = select_domain(
        registry=reg, detection_context=ctx,
        allowed_overrides=frozenset({"general"}),
    )
    assert result.selected_domain == DOMAIN_GENERAL
