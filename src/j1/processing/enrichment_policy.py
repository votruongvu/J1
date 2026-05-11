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
    "SYSTEM_DEFAULT_POLICY",
    "resolve_enrichment_policy",
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
