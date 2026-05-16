"""Deployment-level safety policy for execution profiles.

The user-selectable execution profile lets operators choose
between `minimum_queryable` / `standard` / `advanced` from the UI.
This module is the OPERATOR-level guardrail above that: a
deployment can refuse to run `advanced` (e.g. because the LLM
budget is exhausted), or pin the default profile to
`minimum_queryable` for the on-call dev stack, without changing
the FE picker.

Three concrete rules implemented here:

  1. **Default profile** — used when the REST request omits
     `selectedProfile`. Env: `J1_DEFAULT_INGEST_PROFILE`.
  2. **Allow-list** — set of profiles the deployment will accept.
     A request for a forbidden profile is REJECTED at the REST
     boundary (HTTP 403), never silently downgraded. Envs:
     `J1_ALLOW_MINIMUM_QUERYABLE_INGEST` (default true),
     `J1_ALLOW_STANDARD_INGEST` (default true),
     `J1_ALLOW_ADVANCED_INGEST` (default true).
  3. **No silent coercion** — when the default profile is itself
     not on the allow-list (operator misconfigured), the policy
     surfaces this via `is_consistent()` so deployment health
     checks can fail loudly. The policy never tries to "fix"
     itself by picking a different default.

Design rules (mirror the original refactor brief):

  * **No silent downgrade.** A request for `advanced` when
    `advanced` is forbidden returns a clear 403 with the allowed
    profiles listed — not a swap to `standard`. Operators who
    want auto-downgrade can compose policies on top.
  * **No silent upgrade.** A request for `minimum_queryable`
    when only `advanced` is allowed STILL returns 403 — never
    silently expand the work.
  * **Hygiene at load time.** Bad env values (`J1_DEFAULT_INGEST_PROFILE=premiun`)
    are validated at REST startup and refused — the deployment
    fails to start rather than running with a degraded policy.

Reads env once at module import time would be wrong: tests need
to be able to override the env via the `env=` arg, and worker
bootstrap may want to reload after `J1_*` env updates. Hence the
factory function pattern, matching
[`enrich_assessment_settings`](./enrich_assessment_settings.py).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from j1.processing.execution_profile import (
    DEFAULT_PROFILE,
    ExecutionProfile,
)


ENV_DEFAULT_PROFILE = "J1_DEFAULT_INGEST_PROFILE"
ENV_ALLOW_KNOWLEDGE_INDEX = "J1_ALLOW_KNOWLEDGE_INDEX_INGEST"
# Legacy allow-flags — retained so existing deployments don't fail
# bootstrap. New deployments should set
# ``J1_ALLOW_KNOWLEDGE_INDEX_INGEST`` and ignore these.
ENV_ALLOW_MINIMUM_QUERYABLE = "J1_ALLOW_MINIMUM_QUERYABLE_INGEST"
ENV_ALLOW_STANDARD = "J1_ALLOW_STANDARD_INGEST"
ENV_ALLOW_ADVANCED = "J1_ALLOW_ADVANCED_INGEST"


# Maps every profile to its corresponding "allowed" env var so the
# loader can iterate. Keep in lockstep with `ExecutionProfile`.
_ALLOW_ENV_BY_PROFILE: dict[ExecutionProfile, str] = {
    ExecutionProfile.KNOWLEDGE_INDEX: ENV_ALLOW_KNOWLEDGE_INDEX,
    ExecutionProfile.MINIMUM_QUERYABLE: ENV_ALLOW_MINIMUM_QUERYABLE,
    ExecutionProfile.STANDARD: ENV_ALLOW_STANDARD,
    ExecutionProfile.ADVANCED: ENV_ALLOW_ADVANCED,
}


class InvalidProfilePolicyError(ValueError):
    """Raised at policy load time when the env config is invalid
    (e.g. unknown profile name on `J1_DEFAULT_INGEST_PROFILE`,
    empty allow-list). Distinct exception type so deployment
    bootstrap can catch + log a startup failure cleanly."""


class ProfileNotAllowedError(ValueError):
    """Raised at request time when the caller asked for a profile
    not on the deployment's allow-list. Carries the requested
    profile + the current allow-list so the REST adapter can build
    a clear error message without re-reading env.

    Distinct from `InvalidProfilePolicyError`: this one is a
    request-shape problem (operator + user mismatch), not a
    deployment misconfiguration.
    """

    def __init__(
        self,
        *,
        requested: ExecutionProfile,
        allowed: frozenset[ExecutionProfile],
    ) -> None:
        self.requested = requested
        self.allowed = allowed
        allowed_str = ", ".join(sorted(p.value for p in allowed)) or "(none)"
        super().__init__(
            f"execution profile {requested.value!r} is not allowed by "
            f"this deployment (allowed: {allowed_str})"
        )


@dataclass(frozen=True)
class ExecutionProfilePolicy:
    """Resolved deployment policy for execution profiles.

    Snapshots the current env state at the REST adapter's startup
    so individual requests don't re-read env (which would race
    with hot reloads + complicate test isolation). Operators who
    update env vars must restart the API process — same contract
    every other settings module follows in this codebase.

    Field semantics:

      `default_profile` — applied when the request omits
        `selectedProfile`. ALWAYS a member of `allowed`; the
        loader refuses to construct an inconsistent policy.
      `allowed` — frozenset of acceptable profile values. Empty
        sets are refused at load time (a deployment with no
        allowed profiles cannot ingest, which is almost certainly
        a misconfiguration; better to fail loudly).
    """

    default_profile: ExecutionProfile
    allowed: frozenset[ExecutionProfile]

    def is_allowed(self, profile: ExecutionProfile) -> bool:
        """True iff this profile is on the deployment allow-list."""
        return profile in self.allowed

    def resolve(
        self,
        requested: ExecutionProfile | str | None,
    ) -> tuple[ExecutionProfile, str]:
        """Resolve a caller's profile request to the final profile
        the workflow should execute under.

        Returns `(profile, source)` where `source` is one of:
          * `"rest"`     — caller explicitly chose this profile
          * `"default"`  — caller omitted; deployment default applied

        Raises `ProfileNotAllowedError` when the caller asked for
        a forbidden profile. Never silently downgrades.
        Raises `ValueError` when `requested` is a string that
        doesn't decode to a valid `ExecutionProfile`.
        """
        if requested is None:
            return self.default_profile, "default"
        if isinstance(requested, str):
            profile = ExecutionProfile(requested)
        else:
            profile = requested
        if not self.is_allowed(profile):
            raise ProfileNotAllowedError(
                requested=profile, allowed=self.allowed,
            )
        return profile, "rest"


def load_execution_profile_policy(
    env: Mapping[str, str] | None = None,
) -> ExecutionProfilePolicy:
    """Read the deployment policy from env vars.

    Defaults:

      * Every profile is allowed unless explicitly disabled via
        its `J1_ALLOW_*_INGEST=false` env var.
      * Default profile is `j1.processing.execution_profile.DEFAULT_PROFILE`
        (currently `standard`) unless `J1_DEFAULT_INGEST_PROFILE`
        overrides.

    Raises `InvalidProfilePolicyError` when:

      * `J1_DEFAULT_INGEST_PROFILE` is set to a non-profile
        string (typo).
      * Every profile has been disabled — a deployment that can't
        ingest at all is almost always a misconfiguration.
      * The chosen default is not on the allow-list — the most
        common operator mistake (e.g. `J1_DEFAULT_INGEST_PROFILE=advanced`
        + `J1_ALLOW_ADVANCED_INGEST=false`). Failing at load makes
        this visible at startup rather than on first request.
    """
    src: Mapping[str, str] = env if env is not None else os.environ

    allowed: set[ExecutionProfile] = set()
    for profile, env_name in _ALLOW_ENV_BY_PROFILE.items():
        if _parse_bool(src.get(env_name), default=True):
            allowed.add(profile)
    if not allowed:
        raise InvalidProfilePolicyError(
            "no execution profiles are allowed by the current env "
            f"({', '.join(_ALLOW_ENV_BY_PROFILE.values())} all set to "
            "false). Set at least one to true."
        )

    raw_default = (src.get(ENV_DEFAULT_PROFILE) or "").strip()
    if raw_default:
        try:
            default_profile = ExecutionProfile(raw_default)
        except ValueError as exc:
            raise InvalidProfilePolicyError(
                f"{ENV_DEFAULT_PROFILE}={raw_default!r} is not a "
                f"recognised execution profile (valid values: "
                f"{', '.join(p.value for p in ExecutionProfile)})"
            ) from exc
    else:
        default_profile = DEFAULT_PROFILE

    if default_profile not in allowed:
        raise InvalidProfilePolicyError(
            f"default profile {default_profile.value!r} is not on the "
            f"allow-list (allowed: "
            f"{', '.join(sorted(p.value for p in allowed))}). Either "
            f"enable it via {_ALLOW_ENV_BY_PROFILE[default_profile]}=true "
            f"or set {ENV_DEFAULT_PROFILE} to an allowed profile."
        )

    return ExecutionProfilePolicy(
        default_profile=default_profile,
        allowed=frozenset(allowed),
    )


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    """Mirror the convention used elsewhere in the codebase:
    truthy/falsy strings; anything else falls back to default."""
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    return default
