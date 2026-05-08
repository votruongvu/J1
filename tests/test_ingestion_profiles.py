"""Conformance tests for the IngestProfileDefinition registry.

The registry in `j1.processing.ingestion_profiles` is a documentation
surface — it must stay in sync with the planner's actual decision
table (`_MODE_ENABLED_STEPS`). These tests fail loudly when a new
`IngestMode` ships without a matching profile entry, or when the
registry's enable_* flags drift from the planner's enablement.
"""

from j1.processing.ingestion_profiles import (
    INGEST_PROFILES,
    IngestProfileDefinition,
    expected_steps_for,
    get_profile,
)
from j1.processing.planning import (
    _MODE_ENABLED_STEPS,
    IngestMode,
)


def test_every_ingest_mode_has_a_profile_definition():
    """A new `IngestMode` enum value without a matching entry in
    `INGEST_PROFILES` is a contract violation — operators reading
    `docs/INGESTION_PROFILES.md` would silently miss the new mode."""
    missing = [
        mode.value for mode in IngestMode
        if mode not in INGEST_PROFILES
    ]
    assert not missing, (
        "IngestMode values missing from INGEST_PROFILES registry: "
        f"{missing}"
    )


def test_profile_step_flags_match_planner_mode_enabled_steps():
    """The registry's `enable_compile` / `enable_enrich` / `enable_graph` /
    `enable_index` flags must match `_MODE_ENABLED_STEPS` exactly.
    They're the planner's source of truth for step enablement; the
    registry is the operator-readable projection."""
    for mode, profile in INGEST_PROFILES.items():
        registry_steps = expected_steps_for(mode)
        planner_steps = _MODE_ENABLED_STEPS[mode]
        assert registry_steps == planner_steps, (
            f"Profile registry / planner drift for mode={mode.value}: "
            f"registry says {sorted(registry_steps)}, planner says "
            f"{sorted(planner_steps)}"
        )


def test_get_profile_returns_definition():
    profile = get_profile(IngestMode.TEXT_ONLY)
    assert isinstance(profile, IngestProfileDefinition)
    assert profile.mode is IngestMode.TEXT_ONLY


def test_text_only_profile_avoids_mineru_and_disables_visuals():
    profile = get_profile(IngestMode.TEXT_ONLY)
    assert profile.avoids_mineru is True
    assert profile.enable_image_processing is False
    assert profile.enable_table_processing is False
    assert profile.enable_diagram_processing is False
    assert profile.enable_scanned_page_processing is False
    assert profile.requires_vision is False
    assert profile.cost_level == "low"
    assert profile.latency_level == "fast"


def test_multimodal_full_enables_every_modality():
    profile = get_profile(IngestMode.MULTIMODAL_FULL)
    assert profile.requires_vision is True
    assert profile.enable_image_processing is True
    assert profile.enable_table_processing is True
    assert profile.enable_diagram_processing is True
    assert profile.enable_scanned_page_processing is True
    assert profile.enable_equation_processing is True
    assert profile.cost_level == "high"


def test_full_diagnostic_uses_premium_role():
    profile = get_profile(IngestMode.FULL_DIAGNOSTIC)
    assert profile.text_llm_role == "premium"
    assert profile.operator_notes  # has explicit operator guidance


def test_table_aware_does_not_require_vision():
    profile = get_profile(IngestMode.TABLE_AWARE)
    assert profile.enable_table_processing is True
    assert profile.requires_vision is False
