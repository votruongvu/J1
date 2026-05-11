"""Enrichment policy override resolver.

Implements the precedence chain the spec mandates:

    request override
    > project / profile setting (if available)
    > domain profile default
    > system default

Wave-5 closure left domain policy as the only input; this module
adds the higher-precedence layers so per-run operator overrides
+ per-project defaults can flow into the post-compile analyzer
without modifying domain packs.

The resolver is PURE — no I/O, no LLM, no Temporal coupling. Same
inputs → same `ResolvedEnrichmentPolicy`, every time.

What this module deliberately is NOT:
  * a full enrichment-policy framework with per-task overrides.
    The Wave-5 spec asks for `auto / always / never` as the only
    operator-facing knob. Per-task force/deny lists stay on
    `DomainEnrichmentPolicy`.
  * a config loader. The system default lives as a constant here
    (`SYSTEM_DEFAULT_POLICY`); deployment code injects request /
    project values explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

from j1.domains.models import (
    ENRICHMENT_POLICY_ALWAYS,
    ENRICHMENT_POLICY_AUTO,
    ENRICHMENT_POLICY_NEVER,
    DomainEnrichmentPolicy,
)


__all__ = [
    "POLICY_SOURCE_DOMAIN",
    "POLICY_SOURCE_PROJECT",
    "POLICY_SOURCE_REQUEST",
    "POLICY_SOURCE_SYSTEM_DEFAULT",
    "ResolvedEnrichmentPolicy",
    "ResolvedRequireSuccess",
    "SYSTEM_DEFAULT_POLICY",
    "SYSTEM_DEFAULT_REQUIRE_SUCCESS",
    "resolve_enrichment_policy",
    "resolve_require_enrichment_success",
]


# Stable wire vocabulary — used by `ResolvedEnrichmentPolicy.source`
# so the FE / audit log can render "policy: never (from request)"
# vs "policy: always (from domain default)".
POLICY_SOURCE_REQUEST = "request"
POLICY_SOURCE_PROJECT = "project"
POLICY_SOURCE_DOMAIN = "domain"
POLICY_SOURCE_SYSTEM_DEFAULT = "system_default"


# Backstop when no other layer supplies a policy. Deliberate `auto`
# — production deployments should have a domain default; the
# system default is for tests + edge-case deployments without one.
SYSTEM_DEFAULT_POLICY = ENRICHMENT_POLICY_AUTO

# Wave 7.5 — backstop for the require-enrichment-success flag. Set
# to False because the cautious default is "don't fail the run
# when optional enrichment fails". Deployments that need the
# opposite default flip `J1_ENRICHMENT_REQUIRE_SUCCESS=true` at
# the env layer (consumed via
# `EnrichmentConcurrencySettings.require_enrichment_success`).
SYSTEM_DEFAULT_REQUIRE_SUCCESS = False


_VALID_POLICIES = frozenset({
    ENRICHMENT_POLICY_AUTO,
    ENRICHMENT_POLICY_ALWAYS,
    ENRICHMENT_POLICY_NEVER,
})


@dataclass(frozen=True)
class ResolvedEnrichmentPolicy:
    """The active enrichment policy for one run, plus its provenance.

    `policy` is the literal string (`auto` / `always` / `never`) the
    analyzer + workflow consume. `source` records which precedence
    layer won — operators see "Policy: always (from request)" in
    the FE / final report instead of guessing why it's set."""

    policy: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {"policy": self.policy, "source": self.source}


def resolve_enrichment_policy(
    *,
    request_override: str | None = None,
    project_default: str | None = None,
    domain_policy: DomainEnrichmentPolicy | None = None,
    system_default: str = SYSTEM_DEFAULT_POLICY,
) -> ResolvedEnrichmentPolicy:
    """Resolve the active enrichment policy for one run.

    Precedence (highest first):
      1. `request_override` — operator's per-run choice.
      2. `project_default` — workspace/project-level config.
      3. `domain_policy.policy` — domain pack default.
      4. `system_default` — system backstop (`auto`).

    Invalid values at any layer are skipped (the resolver falls
    through to the next layer) — keeps a typo in one config from
    crashing the workflow. The lowest layer (`system_default`) is
    validated at construction-time; an invalid value there is a
    deployment bug worth raising.
    """
    if _is_valid(request_override):
        return ResolvedEnrichmentPolicy(
            policy=request_override,  # type: ignore[arg-type]
            source=POLICY_SOURCE_REQUEST,
        )
    if _is_valid(project_default):
        return ResolvedEnrichmentPolicy(
            policy=project_default,  # type: ignore[arg-type]
            source=POLICY_SOURCE_PROJECT,
        )
    if domain_policy is not None and _is_valid(domain_policy.policy):
        # Only count the domain layer when it actually expresses an
        # opinion. A pack carrying the default `auto` policy still
        # contributes — operators see "Policy: auto (from domain)".
        return ResolvedEnrichmentPolicy(
            policy=domain_policy.policy,
            source=POLICY_SOURCE_DOMAIN,
        )
    if system_default not in _VALID_POLICIES:
        raise ValueError(
            f"system_default policy {system_default!r} is not one of "
            f"{sorted(_VALID_POLICIES)}; deployment misconfiguration."
        )
    return ResolvedEnrichmentPolicy(
        policy=system_default,
        source=POLICY_SOURCE_SYSTEM_DEFAULT,
    )


def _is_valid(value: str | None) -> bool:
    return isinstance(value, str) and value in _VALID_POLICIES


# ---- Wave 7.5: require_enrichment_success precedence chain ---------


# Source vocabulary for `ResolvedRequireSuccess.source`. Mirrors
# the policy resolver's source labels.
REQUIRE_SUCCESS_SOURCE_REQUEST = POLICY_SOURCE_REQUEST
REQUIRE_SUCCESS_SOURCE_PROJECT = POLICY_SOURCE_PROJECT
REQUIRE_SUCCESS_SOURCE_DOMAIN = POLICY_SOURCE_DOMAIN
REQUIRE_SUCCESS_SOURCE_ENV = "env"
REQUIRE_SUCCESS_SOURCE_SYSTEM_DEFAULT = POLICY_SOURCE_SYSTEM_DEFAULT


@dataclass(frozen=True)
class ResolvedRequireSuccess:
    """The resolved `require_enrichment_success` flag for one run.

    Distinct from the policy literal (`auto / always / never`) — a
    domain pack can have `policy=always` AND
    `require_enrichment_success=False` (recommend enrichment but
    don't fail the run if the LLM is down). The FE renders the
    source so operators see why the run was treated as
    require-success."""

    require_enrichment_success: bool
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "require_enrichment_success": self.require_enrichment_success,
            "source": self.source,
        }


def resolve_require_enrichment_success(
    *,
    request_override: bool | None = None,
    project_default: bool | None = None,
    domain_policy: DomainEnrichmentPolicy | None = None,
    env_default: bool | None = None,
    system_default: bool = SYSTEM_DEFAULT_REQUIRE_SUCCESS,
) -> ResolvedRequireSuccess:
    """Resolve the active `require_enrichment_success` flag.

    Precedence (highest first):
      1. `request_override` — operator's per-run choice.
         Pass None (default) when the request shape doesn't carry
         the flag yet; the resolver falls through.
      2. `project_default` — workspace/project-level config.
      3. `domain_policy.require_enrichment_success` — pack opinion.
         A pack with the default `False` STILL counts as having
         expressed an opinion — operators see "from domain" in the
         source label. Pack-absent runs fall through.
      4. `env_default` — env-level fallback
         (`J1_ENRICHMENT_REQUIRE_SUCCESS`, surfaced via
         `EnrichmentConcurrencySettings.require_enrichment_success`).
      5. `system_default` — hardcoded False backstop.

    Distinct precedence from the policy resolver — this is the
    spec's:

        request override, if available
        > domain/profile policy
        > env/config default
        > system default false

    Note: the spec puts `domain/profile` ABOVE `env` here, in
    contrast to the policy resolver which puts `project_default`
    above `domain_policy`. The spec's intent: operators set env
    values to STANDARDIZE deployment behaviour, but a domain pack
    that explicitly declares its requirement (e.g. "regulated data
    domain — enrichment failure = run failure") should win over a
    generic env default. Implementation: domain layer wins over
    env_default; request/project still win over both."""
    if request_override is not None:
        return ResolvedRequireSuccess(
            require_enrichment_success=bool(request_override),
            source=REQUIRE_SUCCESS_SOURCE_REQUEST,
        )
    if project_default is not None:
        return ResolvedRequireSuccess(
            require_enrichment_success=bool(project_default),
            source=REQUIRE_SUCCESS_SOURCE_PROJECT,
        )
    if domain_policy is not None and _domain_pack_expresses_opinion(domain_policy):
        return ResolvedRequireSuccess(
            require_enrichment_success=bool(
                domain_policy.require_enrichment_success,
            ),
            source=REQUIRE_SUCCESS_SOURCE_DOMAIN,
        )
    if env_default is not None:
        return ResolvedRequireSuccess(
            require_enrichment_success=bool(env_default),
            source=REQUIRE_SUCCESS_SOURCE_ENV,
        )
    return ResolvedRequireSuccess(
        require_enrichment_success=bool(system_default),
        source=REQUIRE_SUCCESS_SOURCE_SYSTEM_DEFAULT,
    )


def _domain_pack_expresses_opinion(
    policy: DomainEnrichmentPolicy,
) -> bool:
    """A domain pack 'expresses an opinion' about
    require_enrichment_success when:
      * its policy literal is something other than `auto` (i.e. the
        pack is deliberately marked always/never), OR
      * its `require_enrichment_success` field is True.

    A pack defaulting to (policy=auto, require_success=False) is
    a no-op overlay — the resolver falls through to the env layer
    so deployments can express a fleet-wide default without every
    pack having to opt in."""
    if policy.require_enrichment_success:
        return True
    if policy.policy != ENRICHMENT_POLICY_AUTO:
        return True
    return False
