"""Phase 2 follow-up (PR-03): legacy `J1_INGEST_PLANNER_ENABLED` env
name compatibility.

The var was renamed to `J1_ASSESSMENT_ENABLED` in Phase 2. Deployments
that still set the legacy name must keep working (value honoured)
AND must see a clear deprecation warning at bootstrap so operators
can migrate before the fallback is removed.
"""

from __future__ import annotations

import logging

import pytest

from deploy.dev.api import (
    _ENV_ASSESSMENT_ENABLED,
    _ENV_LEGACY_PLANNER_ENABLED,
    resolve_assessment_enabled,
)


# ---- Happy path: new name wins, no warning ----------------------


def test_new_name_set_to_true_returns_true_with_no_warning(caplog):
    caplog.set_level(logging.WARNING, logger="j1.dev.api")
    assert resolve_assessment_enabled(
        {_ENV_ASSESSMENT_ENABLED: "true"},
    ) is True
    assert caplog.records == []


def test_new_name_set_to_false_returns_false_with_no_warning(caplog):
    caplog.set_level(logging.WARNING, logger="j1.dev.api")
    assert resolve_assessment_enabled(
        {_ENV_ASSESSMENT_ENABLED: "false"},
    ) is False
    assert caplog.records == []


def test_neither_env_set_returns_default_true_with_no_warning(caplog):
    caplog.set_level(logging.WARNING, logger="j1.dev.api")
    assert resolve_assessment_enabled({}) is True
    assert caplog.records == []


# ---- Legacy-only path: value honoured, warning logged -----------


def test_legacy_env_name_logs_warning_and_is_honoured_true(caplog):
    """When only the legacy `J1_INGEST_PLANNER_ENABLED` is set, the
    bootstrap MUST honour its value AND emit a deprecation warning
    pointing operators at the new name. Without the warning the
    rename is silent and operators don't migrate."""
    caplog.set_level(logging.WARNING, logger="j1.dev.api")
    result = resolve_assessment_enabled(
        {_ENV_LEGACY_PLANNER_ENABLED: "true"},
    )
    assert result is True
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert _ENV_LEGACY_PLANNER_ENABLED in record.message
    assert _ENV_ASSESSMENT_ENABLED in record.message
    assert "deprecated" in record.message.lower()


def test_legacy_env_name_logs_warning_and_is_honoured_false(caplog):
    """The legacy value flows through verbatim — a legacy
    `J1_INGEST_PLANNER_ENABLED=false` MUST disable assessment, not
    silently flip to the default-on behaviour of the new name."""
    caplog.set_level(logging.WARNING, logger="j1.dev.api")
    result = resolve_assessment_enabled(
        {_ENV_LEGACY_PLANNER_ENABLED: "false"},
    )
    assert result is False
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "deprecated" in record.message.lower()


@pytest.mark.parametrize("falsy", ["false", "0", "no", "off", "FALSE"])
def test_legacy_env_falsy_variants_resolve_to_false(caplog, falsy: str):
    """Match the truthy/falsy parser used for the new var so a
    pre-rename operator who set `OFF` doesn't see behaviour change
    after the rename."""
    caplog.set_level(logging.WARNING, logger="j1.dev.api")
    result = resolve_assessment_enabled(
        {_ENV_LEGACY_PLANNER_ENABLED: falsy},
    )
    assert result is False


# ---- Conflict path: new name wins, conflict warning ------------


def test_both_envs_set_prefers_new_name_and_warns_about_conflict(caplog):
    """When both names are set the new name wins, but a conflict
    warning fires so the operator deletes the stale legacy entry
    instead of leaving it to rot in the env file."""
    caplog.set_level(logging.WARNING, logger="j1.dev.api")
    result = resolve_assessment_enabled({
        _ENV_ASSESSMENT_ENABLED: "false",
        _ENV_LEGACY_PLANNER_ENABLED: "true",
    })
    # New name wins → assessment disabled.
    assert result is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].message
    assert _ENV_ASSESSMENT_ENABLED in msg
    assert _ENV_LEGACY_PLANNER_ENABLED in msg
    assert "Both" in msg or "both" in msg


def test_both_envs_set_with_agreement_still_warns(caplog):
    """Conflict warning fires even when the two values agree —
    operators should still be told to clean up the legacy entry."""
    caplog.set_level(logging.WARNING, logger="j1.dev.api")
    result = resolve_assessment_enabled({
        _ENV_ASSESSMENT_ENABLED: "true",
        _ENV_LEGACY_PLANNER_ENABLED: "true",
    })
    assert result is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert _ENV_LEGACY_PLANNER_ENABLED in warnings[0].message
