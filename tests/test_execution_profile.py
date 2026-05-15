"""Tests for the user-selectable `ExecutionProfile` data model.

Pins the contract every downstream gate reads from:

 * the capability matrix is the only source of truth for what a
   profile does/doesn't allow
 * `minimum_queryable` is honestly minimal — no enrich, no graph
   build, no multimodal, LightRAG entity extraction disabled
 * `standard` does NOT enable enrich or graph build by default;
   it disables them at the workflow gate but cannot disable the
   library-internal extraction (the matrix is explicit about this)
 * `advanced` enables everything
 * the recommendation function never returns `minimum_queryable`
   automatically — that's always an opt-in debugging choice
 * audit payload round-trips via `ExecutionProfileSelection.to_payload()`

Pure unit tests — no I/O, no Temporal, no LLM.
"""

from __future__ import annotations

import pytest

from j1.processing.execution_profile import (
    DEFAULT_PROFILE,
    PROFILE_CAPABILITIES,
    PROFILE_LABELS,
    SCHEMA_VERSION,
    SELECTED_BY_RECOMMENDATION,
    SELECTED_BY_USER,
    SELECTION_SOURCE_REST,
    SELECTION_SOURCE_UI,
    ExecutionProfile,
    ExecutionProfileSelection,
    ProfileCapabilities,
    capabilities_for,
    profile_details,
    recommend_profile_from_assessment,
)


# ---- Capability matrix invariants --------------------------------


def test_every_profile_has_capabilities():
    for profile in ExecutionProfile:
        assert profile in PROFILE_CAPABILITIES, (
            f"{profile} missing from PROFILE_CAPABILITIES — "
            f"the matrix is the source of truth; every profile must be listed."
        )


def test_minimum_queryable_is_honestly_minimal():
    caps = capabilities_for(ExecutionProfile.MINIMUM_QUERYABLE)
    assert caps.run_enrich is False
    assert caps.run_graph_build is False
    assert caps.compile_multimodal_processing is False
    assert caps.compile_lightrag_entity_extraction is False
    assert caps.compile_lightrag_relationship_extraction is False
    assert caps.domain_enrichment is False
    assert caps.validation_tasks is False
    # Index must remain on — otherwise the document is not queryable.
    assert caps.run_index is True


def test_standard_does_not_enable_enrich_or_graph_by_default():
    caps = capabilities_for(ExecutionProfile.STANDARD)
    assert caps.run_enrich is False
    assert caps.run_graph_build is False
    assert caps.run_index is True
    # `standard` does NOT pretend to disable library-internal
    # extraction — the matrix is honest about it.
    assert caps.compile_lightrag_entity_extraction is True
    assert caps.compile_lightrag_relationship_extraction is True


def test_advanced_enables_everything():
    caps = capabilities_for(ExecutionProfile.ADVANCED)
    for flag in (
        caps.run_enrich,
        caps.run_graph_build,
        caps.run_index,
        caps.compile_multimodal_processing,
        caps.enrich_image_captioning,
        caps.enrich_vision_enrichment,
        caps.enrich_table_enrichment,
        caps.domain_enrichment,
        caps.validation_tasks,
    ):
        assert flag is True


def test_capabilities_for_raises_on_unknown():
    with pytest.raises(ValueError, match="unknown ExecutionProfile"):
        capabilities_for("does_not_exist")  # type: ignore[arg-type]


# ---- Profile details payload -------------------------------------


def test_profile_details_keys_are_stable_for_minimum_queryable():
    details = profile_details(ExecutionProfile.MINIMUM_QUERYABLE)
    assert details["id"] == "minimum_queryable"
    assert details["queryable"] is True
    assert details["expected_speed"] == "fast"
    assert details["expected_llm_usage"] == "none_or_minimal"
    assert details["graph_enabled"] is False
    assert details["multimodal_processing"] is False
    assert details["enrichment_enabled"] is False
    assert details["compile_lightrag_extraction"] is False


def test_profile_details_keys_are_stable_for_advanced():
    details = profile_details(ExecutionProfile.ADVANCED)
    assert details["id"] == "advanced"
    assert details["expected_speed"] == "slow"
    assert details["expected_llm_usage"] == "high"
    assert details["graph_enabled"] is True
    assert details["compile_lightrag_extraction"] is True


def test_profile_details_standard_discloses_lightrag_cost():
    details = profile_details(ExecutionProfile.STANDARD)
    # Honesty test: `standard` users must see that entity extraction
    # still fires inside compile, even though the workflow's
    # graph-build stage is skipped.
    assert details["compile_lightrag_extraction"] is True
    assert details["graph_enabled"] is False


# ---- Recommendation rules ----------------------------------------


def test_recommend_scanned_pdf_picks_advanced():
    profile, reasons = recommend_profile_from_assessment(
        has_images=False,
        has_tables=False,
        has_scanned_pages=True,
        text_extractable_ratio=0.0,
        page_count=5,
    )
    assert profile == ExecutionProfile.ADVANCED
    assert any("scanned" in r.lower() for r in reasons)


def test_recommend_low_text_extractability_picks_advanced():
    profile, _ = recommend_profile_from_assessment(
        has_images=False,
        has_tables=False,
        has_scanned_pages=False,
        text_extractable_ratio=0.05,
        page_count=10,
    )
    assert profile == ExecutionProfile.ADVANCED


def test_recommend_images_or_tables_picks_advanced_for_short_doc():
    profile, reasons = recommend_profile_from_assessment(
        has_images=True,
        has_tables=False,
        has_scanned_pages=False,
        text_extractable_ratio=0.9,
        page_count=20,
    )
    assert profile == ExecutionProfile.ADVANCED
    assert any("images" in r for r in reasons)


def test_recommend_images_on_long_doc_downgrades_to_standard():
    """Long multimodal docs default to `standard` so the operator
    opts in to `advanced` explicitly — full enrichment on a 300-page
    PDF would be hours."""
    profile, reasons = recommend_profile_from_assessment(
        has_images=True,
        has_tables=True,
        has_scanned_pages=False,
        text_extractable_ratio=0.95,
        page_count=300,
    )
    assert profile == ExecutionProfile.STANDARD
    assert any("long" in r.lower() for r in reasons)


def test_recommend_text_only_picks_standard():
    profile, _ = recommend_profile_from_assessment(
        has_images=False,
        has_tables=False,
        has_scanned_pages=False,
        text_extractable_ratio=0.99,
        page_count=15,
    )
    assert profile == ExecutionProfile.STANDARD


def test_recommend_never_returns_minimum_queryable():
    """`minimum_queryable` is debugging-only — never recommended
    automatically. Sweep the input space."""
    for has_images in (False, True):
        for has_tables in (False, True):
            for ratio in (0.05, 0.5, 0.99):
                for pages in (1, 50, 500):
                    profile, _ = recommend_profile_from_assessment(
                        has_images=has_images,
                        has_tables=has_tables,
                        has_scanned_pages=False,
                        text_extractable_ratio=ratio,
                        page_count=pages,
                    )
                    assert profile != ExecutionProfile.MINIMUM_QUERYABLE


# ---- Audit payload round-trip ------------------------------------


def test_selection_payload_round_trip():
    selection = ExecutionProfileSelection(
        recommended_profile=ExecutionProfile.ADVANCED,
        selected_profile=ExecutionProfile.MINIMUM_QUERYABLE,
        selected_by=SELECTED_BY_USER,
        selection_source=SELECTION_SOURCE_UI,
        reasons=("Document has tables",),
        warnings=(),
    )
    payload = selection.to_payload()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["assessment_recommended_profile"] == "advanced"
    assert payload["selected_execution_profile"] == "minimum_queryable"
    assert payload["profile_selected_by"] == "user"
    assert payload["profile_selection_source"] == "ui"
    assert payload["profile_reasons"] == ["Document has tables"]
    assert payload["profile_warnings"] == []


def test_selection_payload_when_user_accepts_recommendation():
    selection = ExecutionProfileSelection(
        recommended_profile=ExecutionProfile.STANDARD,
        selected_profile=ExecutionProfile.STANDARD,
        selected_by=SELECTED_BY_RECOMMENDATION,
        selection_source=SELECTION_SOURCE_UI,
    )
    payload = selection.to_payload()
    assert payload["profile_selected_by"] == "recommendation"
    assert payload["selected_execution_profile"] == payload["assessment_recommended_profile"]


# ---- Wire-string stability ---------------------------------------


def test_profile_wire_strings_are_stable():
    """Dashboards filter on these strings; renaming is a migration.
    Pin them in tests so a casual rename trips CI."""
    assert ExecutionProfile.MINIMUM_QUERYABLE.value == "minimum_queryable"
    assert ExecutionProfile.STANDARD.value == "standard"
    assert ExecutionProfile.ADVANCED.value == "advanced"


def test_profile_labels_cover_every_profile():
    for profile in ExecutionProfile:
        assert profile in PROFILE_LABELS
        assert PROFILE_LABELS[profile], "label must be non-empty"


def test_default_profile_is_standard():
    """Until the FE selection lands, `standard` is the safe default.
    Pinned in tests so a flip to `MINIMUM_QUERYABLE` is intentional."""
    assert DEFAULT_PROFILE == ExecutionProfile.STANDARD


# ---- detect_unsupported_controls --------------------------------


def test_detect_unsupported_controls_empty_when_adapter_honored_everything():
    """When the adapter reports no unhandled capabilities, no
    profile controls are flagged unsupported — regardless of
    which profile is active."""
    from j1.processing.execution_profile import detect_unsupported_controls

    for profile in ExecutionProfile:
        controls = detect_unsupported_controls(
            profile=profile,
            unhandled_capabilities=(),
        )
        assert controls == ()


def test_detect_unsupported_controls_flags_multimodal_for_minimum_queryable():
    """`minimum_queryable` requests `compile_multimodal_processing=False`.
    If the adapter couldn't enforce it (e.g. the installed
    RAGAnythingConfig doesn't expose `enable_image_processing`),
    we MUST surface that to the operator so the profile isn't
    a fiction."""
    from j1.processing.execution_profile import detect_unsupported_controls

    controls = detect_unsupported_controls(
        profile=ExecutionProfile.MINIMUM_QUERYABLE,
        unhandled_capabilities=("image_extraction",),
    )
    assert len(controls) == 1
    payload = controls[0].to_payload()
    assert payload["control"] == "disable_multimodal_processing"
    assert payload["requested_value"] is True
    assert "RAGAnythingConfig" in payload["reason"]
    assert "minimum_queryable" in payload["impact"]


def test_detect_unsupported_controls_silent_on_advanced_profile():
    """When `advanced` is active the profile PERMITS multimodal
    processing — an unhandled `image_extraction` capability is
    informational at most, not a profile violation. We do not
    pollute the operator-facing warning list with non-violations."""
    from j1.processing.execution_profile import detect_unsupported_controls

    controls = detect_unsupported_controls(
        profile=ExecutionProfile.ADVANCED,
        unhandled_capabilities=("image_extraction",),
    )
    assert controls == ()


def test_detect_unsupported_controls_accepts_list_input():
    """The adapter today emits a list (not a tuple) on the
    metadata bridge — must accept either."""
    from j1.processing.execution_profile import detect_unsupported_controls

    controls = detect_unsupported_controls(
        profile=ExecutionProfile.MINIMUM_QUERYABLE,
        unhandled_capabilities=["image_extraction"],
    )
    assert len(controls) == 1


def test_detect_unsupported_controls_ignores_unrelated_tokens():
    """Tokens that don't correspond to any profile-controllable
    capability are skipped — they belong on the existing
    `unhandled_capabilities` field, not on the profile-specific
    warning surface."""
    from j1.processing.execution_profile import detect_unsupported_controls

    controls = detect_unsupported_controls(
        profile=ExecutionProfile.MINIMUM_QUERYABLE,
        unhandled_capabilities=("some_future_capability", "another_one"),
    )
    assert controls == ()


def test_unsupported_profile_control_payload_shape_is_stable():
    """Wire-string field names persisted on
    `IngestionRun.metadata.unsupported_profile_controls`. Renaming
    is a migration."""
    from j1.processing.execution_profile import UnsupportedProfileControl

    ctrl = UnsupportedProfileControl(
        control="disable_multimodal_processing",
        requested_value=True,
        reason="r",
        impact="i",
    )
    assert ctrl.to_payload() == {
        "control": "disable_multimodal_processing",
        "requested_value": True,
        "reason": "r",
        "impact": "i",
    }
