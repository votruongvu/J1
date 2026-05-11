"""Generic domain pack.

The fallback that's always selected when no domain pack scores
above threshold (and no operator override is in play). Carries no
keywords, no overlays, no extraction targets — its job is to give
the registry a stable id (`general`) so consumers don't special-
case 'no domain'."""

from __future__ import annotations

from j1.domains.models import DomainPack
from j1.domains.registry import DOMAIN_GENERAL


__all__ = ["build_general_pack"]


GENERIC_PROMPT_ADDON = """\
This document does not appear to belong to a specialised domain.
Use the generic ingestion planning rules — prefer fast/balanced
profiles, do not enable expensive domain-specific extractors
without strong evidence."""


def build_general_pack() -> DomainPack:
    """Construct the generic fallback pack.

 A no-op detector — the registry skips packs whose `detect=None`,
 so generic never competes with domain packs in auto-detection.
 """
    return DomainPack(
        id=DOMAIN_GENERAL,
        display_name="Generic",
        version="generic",
        prompt_addon=GENERIC_PROMPT_ADDON,
        detect=None,
    )
