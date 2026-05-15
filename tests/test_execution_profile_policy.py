"""Tests for the deployment-level execution-profile safety policy.

Pins the operator-facing guardrail contract:

  * Bad env config fails at LOAD time, not at first request —
    operator sees the startup failure instead of a degraded
    runtime.
  * Rejection of forbidden profiles is EXPLICIT (raises a
    structured error) — never a silent downgrade.
  * Default profile applies only when caller omits selection;
    explicit selection takes precedence (unless forbidden).
  * Default MUST be on the allow-list — anything else is an
    operator misconfiguration.

Pure unit tests — pass `env=` mappings; no environ mutation.
"""

from __future__ import annotations

import pytest

from j1.processing.execution_profile import ExecutionProfile
from j1.processing.execution_profile_policy import (
    ENV_ALLOW_ADVANCED,
    ENV_ALLOW_MINIMUM_QUERYABLE,
    ENV_ALLOW_STANDARD,
    ENV_DEFAULT_PROFILE,
    ExecutionProfilePolicy,
    InvalidProfilePolicyError,
    ProfileNotAllowedError,
    load_execution_profile_policy,
)


# ---- load_execution_profile_policy ------------------------------


def test_default_load_allows_every_profile_and_defaults_to_standard():
    """Out-of-the-box deployment: every profile allowed, default
    is `standard`. Pinned so an accidental flip of the safe-default
    is intentional."""
    policy = load_execution_profile_policy(env={})
    assert policy.allowed == frozenset(ExecutionProfile)
    assert policy.default_profile == ExecutionProfile.STANDARD


def test_load_respects_advanced_disable():
    """`J1_ALLOW_ADVANCED_INGEST=false` strips `advanced` from
    the allow-list. Other profiles untouched."""
    policy = load_execution_profile_policy(
        env={ENV_ALLOW_ADVANCED: "false"},
    )
    assert ExecutionProfile.ADVANCED not in policy.allowed
    assert ExecutionProfile.STANDARD in policy.allowed
    assert ExecutionProfile.MINIMUM_QUERYABLE in policy.allowed


def test_load_respects_minimum_queryable_disable():
    """Some prod deployments may forbid the debug-only profile."""
    policy = load_execution_profile_policy(
        env={ENV_ALLOW_MINIMUM_QUERYABLE: "false"},
    )
    assert ExecutionProfile.MINIMUM_QUERYABLE not in policy.allowed


def test_load_custom_default_profile():
    policy = load_execution_profile_policy(
        env={ENV_DEFAULT_PROFILE: "minimum_queryable"},
    )
    assert policy.default_profile == ExecutionProfile.MINIMUM_QUERYABLE


def test_load_rejects_unknown_default_profile():
    """Typo on the env var fails loudly at startup — not silently
    falls back to STANDARD. This is the operator's safety net."""
    with pytest.raises(InvalidProfilePolicyError) as exc_info:
        load_execution_profile_policy(
            env={ENV_DEFAULT_PROFILE: "premiun"},
        )
    msg = str(exc_info.value)
    assert "premiun" in msg
    # The error message lists valid values so the operator doesn't
    # have to grep the codebase.
    assert "minimum_queryable" in msg
    assert "standard" in msg
    assert "advanced" in msg


def test_load_rejects_empty_allow_list():
    """If every profile is disabled, the deployment can't ingest
    anything. Fail at load rather than 503-ing every request."""
    with pytest.raises(InvalidProfilePolicyError):
        load_execution_profile_policy(env={
            ENV_ALLOW_MINIMUM_QUERYABLE: "false",
            ENV_ALLOW_STANDARD: "false",
            ENV_ALLOW_ADVANCED: "false",
        })


def test_load_rejects_default_not_on_allow_list():
    """Most common operator mistake: default points at a profile
    you just disabled. Fail at load so the bad combo is visible
    on the first deploy, not the first user request."""
    with pytest.raises(InvalidProfilePolicyError) as exc_info:
        load_execution_profile_policy(env={
            ENV_DEFAULT_PROFILE: "advanced",
            ENV_ALLOW_ADVANCED: "false",
        })
    msg = str(exc_info.value)
    assert "advanced" in msg
    # Error message hints at the two ways to fix.
    assert ENV_ALLOW_ADVANCED in msg or "allow-list" in msg


def test_load_accepts_truthy_string_variants():
    """The bool parser accepts canonical truthy/falsy strings.
    Pin the matrix so an operator's `J1_ALLOW_ADVANCED_INGEST=yes`
    doesn't silently fall through to the default."""
    for value in ("true", "1", "yes", "on", "TRUE", "True"):
        policy = load_execution_profile_policy(env={
            ENV_ALLOW_ADVANCED: value,
        })
        assert ExecutionProfile.ADVANCED in policy.allowed
    for value in ("false", "0", "no", "off", "FALSE", "False"):
        policy = load_execution_profile_policy(env={
            ENV_ALLOW_ADVANCED: value,
        })
        assert ExecutionProfile.ADVANCED not in policy.allowed


# ---- ExecutionProfilePolicy.resolve -----------------------------


def _open_policy(
    allowed: set[ExecutionProfile] | None = None,
    default: ExecutionProfile = ExecutionProfile.STANDARD,
) -> ExecutionProfilePolicy:
    """Construct a policy directly (bypassing env-load) so resolve
    tests don't have to thread env mappings."""
    return ExecutionProfilePolicy(
        default_profile=default,
        allowed=frozenset(allowed if allowed is not None else ExecutionProfile),
    )


def test_resolve_none_returns_default_with_default_source():
    policy = _open_policy()
    profile, source = policy.resolve(None)
    assert profile == ExecutionProfile.STANDARD
    assert source == "default"


def test_resolve_explicit_value_returns_rest_source():
    """Source distinguishes "user chose this" from "deployment
    default kicked in" — important for the audit trail."""
    policy = _open_policy()
    profile, source = policy.resolve(ExecutionProfile.MINIMUM_QUERYABLE)
    assert profile == ExecutionProfile.MINIMUM_QUERYABLE
    assert source == "rest"


def test_resolve_accepts_wire_string():
    """REST passes wire strings, not enums. Both paths must work."""
    policy = _open_policy()
    profile, source = policy.resolve("advanced")
    assert profile == ExecutionProfile.ADVANCED
    assert source == "rest"


def test_resolve_rejects_forbidden_profile_explicitly():
    """No silent downgrade. The structured error carries the
    allowed list so the REST adapter can build a useful 403 body
    without re-reading env."""
    policy = _open_policy(
        allowed={ExecutionProfile.MINIMUM_QUERYABLE, ExecutionProfile.STANDARD},
    )
    with pytest.raises(ProfileNotAllowedError) as exc_info:
        policy.resolve(ExecutionProfile.ADVANCED)
    err = exc_info.value
    assert err.requested == ExecutionProfile.ADVANCED
    assert ExecutionProfile.ADVANCED not in err.allowed
    assert ExecutionProfile.STANDARD in err.allowed
    # Message lists the valid alternatives.
    assert "minimum_queryable" in str(err)
    assert "standard" in str(err)


def test_resolve_rejects_unknown_wire_string():
    """Distinct from `ProfileNotAllowedError`: this is a malformed
    request, not a policy violation. Raise the standard ValueError
    so REST returns 400 not 403."""
    policy = _open_policy()
    with pytest.raises(ValueError):
        policy.resolve("does_not_exist")


def test_resolve_does_not_silently_upgrade():
    """When the deployment ONLY allows `advanced`, a request for
    `minimum_queryable` is STILL rejected — never silently upgraded.
    This is the symmetric case to the more common downgrade
    rejection."""
    policy = _open_policy(allowed={ExecutionProfile.ADVANCED})
    with pytest.raises(ProfileNotAllowedError):
        policy.resolve(ExecutionProfile.MINIMUM_QUERYABLE)


# ---- is_allowed ---------------------------------------------------


def test_is_allowed_matrix():
    policy = _open_policy(
        allowed={ExecutionProfile.STANDARD, ExecutionProfile.ADVANCED},
    )
    assert policy.is_allowed(ExecutionProfile.STANDARD)
    assert policy.is_allowed(ExecutionProfile.ADVANCED)
    assert not policy.is_allowed(ExecutionProfile.MINIMUM_QUERYABLE)
