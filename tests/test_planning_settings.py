"""Tests for `j1.processing.planning_settings.load_planning_settings`."""

from __future__ import annotations

import pytest

from j1.errors.exceptions import ConfigError
from j1.processing.planning_settings import (
    PlanningSettings,
    load_planning_settings,
)


def test_defaults_when_env_empty():
    """Default behaviour: LLM-assisted planning OFF, FAST role profile
 with conservative digest caps. These defaults define the baseline
 contract — flipping them in code should be a deliberate,
 separately-reviewed change."""
    s = load_planning_settings({})
    assert s == PlanningSettings(
        llm_planning_enabled=False,
        model_profile="fast_planner",
        max_sample_blocks=20,
        max_preview_chars=300,
    )


def test_explicit_overrides():
    s = load_planning_settings({
        "J1_LLM_PLANNING_ENABLED": "true",
        "J1_PLANNING_MODEL_PROFILE": "premium_planner",
        "J1_PLANNING_MAX_SAMPLE_BLOCKS": "5",
        "J1_PLANNING_MAX_PREVIEW_CHARS": "120",
    })
    assert s.llm_planning_enabled is True
    assert s.model_profile == "premium_planner"
    assert s.max_sample_blocks == 5
    assert s.max_preview_chars == 120


@pytest.mark.parametrize("raw,expected", [
    ("yes", True), ("on", True), ("1", True),
    ("no", False), ("off", False), ("0", False),
    ("TRUE", True), ("FaLsE", False),
])
def test_bool_parsing_accepts_common_synonyms(raw, expected):
    s = load_planning_settings({"J1_LLM_PLANNING_ENABLED": raw})
    assert s.llm_planning_enabled is expected


def test_bool_parsing_rejects_garbage():
    with pytest.raises(ConfigError) as exc:
        load_planning_settings({"J1_LLM_PLANNING_ENABLED": "maybe"})
    assert "J1_LLM_PLANNING_ENABLED" in str(exc.value)


def test_int_parsing_rejects_non_positive():
    with pytest.raises(ConfigError):
        load_planning_settings({"J1_PLANNING_MAX_SAMPLE_BLOCKS": "0"})
    with pytest.raises(ConfigError):
        load_planning_settings({"J1_PLANNING_MAX_PREVIEW_CHARS": "-1"})


def test_int_parsing_rejects_non_numeric():
    with pytest.raises(ConfigError):
        load_planning_settings({"J1_PLANNING_MAX_SAMPLE_BLOCKS": "abc"})


def test_empty_string_falls_back_to_default():
    s = load_planning_settings({
        "J1_PLANNING_MODEL_PROFILE": "",
        "J1_PLANNING_MAX_SAMPLE_BLOCKS": "",
    })
    assert s.model_profile == "fast_planner"
    assert s.max_sample_blocks == 20


# ---- J1_LLM_PLANNING_ENABLED ---------------------------------------


def test_default_llm_planning_disabled():
    """LLM-assisted planning is opt-in. The default keeps the
    rule-based path so deployments without a planner LLM wired
    don't silently spend on every ingest."""
    s = load_planning_settings({})
    assert s.llm_planning_enabled is False


def test_llm_planning_enabled_flag_turns_it_on():
    s = load_planning_settings({"J1_LLM_PLANNING_ENABLED": "true"})
    assert s.llm_planning_enabled is True


def test_llm_planning_enabled_flag_off_keeps_it_off():
    s = load_planning_settings({"J1_LLM_PLANNING_ENABLED": "false"})
    assert s.llm_planning_enabled is False
