"""Domain query augmentation provider.

The provider is the Phase-3 seam where retrieval / synthesis can
ask "what does the active domain pack say about this query?"
without reaching into ``DomainPack`` internals. It is intentionally
small: the goal of Phase 3 is to define the contract and the
domain-pack-backed default, NOT to change retrieval quality.

Contract:

* Inputs come from a ``DocumentMemoryView`` (the Unified Memory
  projection) plus the user's question.
* Outputs are pure-data hints — terms, aliases, recommended query
  expansions. Callers decide whether to use them.
* No I/O. No LLM calls. No state.

Sources, in precedence order:

1. **Static domain pack hints** — ``DomainPack.extraction_hints``
   (``terminology_hints``, ``retrieval_hints``). Loaded from the
   pack registry at construction time.
2. **(Future)** Enrichment-generated aliases from the active
   snapshot's ``enriched.*`` artifacts. This phase exposes the
   shape but does not yet read the artifacts — the resolver will
   surface the count via ``DocumentMemoryView.aliases_count``;
   a future Phase-4 entity-alias provider reads the bodies.

Feature flag: ``J1_DOMAIN_QUERY_AUGMENTATION_ENABLED`` (default
``true``). When disabled, the provider returns empty tuples for
every query so callers that wire the provider into retrieval can
A/B compare ON vs OFF without touching code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from j1.domains.models import DomainPack
    from j1.memory.unified import DocumentMemoryView


__all__ = [
    "AugmentationHints",
    "DomainQueryAugmentationProvider",
    "DomainPackAugmentationProvider",
    "NoOpAugmentationProvider",
    "ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED",
    "is_augmentation_enabled",
]


ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED = "J1_DOMAIN_QUERY_AUGMENTATION_ENABLED"


def is_augmentation_enabled(env: dict[str, str] | None = None) -> bool:
    """Read the feature flag. Defaults to ``true`` — the augmentation
    surface is on by default, but its current outputs are advisory
    (callers can ignore them without correctness impact)."""
    source = env if env is not None else os.environ
    raw = source.get(ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED)
    if raw is None:
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AugmentationHints:
    """Pure-data response from the augmentation provider.

    Every field is advisory. Retrieval / synthesis callers MAY use
    them to broaden lookup terms, hint a rerank, or surface
    diagnostics. None of them MUST be applied — the original user
    query is always the source of truth for the answer.

    * ``domain_terms``: canonical glossary entries from the active
      domain pack. Useful for surfacing in dev panels.
    * ``aliases``: synonym / abbreviation pairs the domain knows
      about, ``(short, long)`` style.
    * ``recommended_expansions``: terms the provider thinks the
      retriever should ALSO look up. A safe default expansion is
      ``aliases`` themselves, but a richer provider may suggest
      narrower expansions per question. Capped on the consumer
      side — providers should keep the list tight (~5 entries).
    * ``source``: ``"domain_pack"`` / ``"enrichment"`` /
      ``"disabled"``. Lets the FE render "augmentation came from
      the domain pack" in the diagnostic panel.
    """

    domain_terms: tuple[str, ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()
    recommended_expansions: tuple[str, ...] = ()
    source: str = "disabled"


class DomainQueryAugmentationProvider(Protocol):
    """Surface every augmentation provider implements.

    The interface is intentionally minimal — Phase 3 ships a
    domain-pack-backed default; Phase 4 will add an
    enrichment-aware provider that fans out across both sources.
    """

    def hints_for(
        self,
        memory_view: "DocumentMemoryView",
        query: str,
    ) -> AugmentationHints:
        """Return augmentation hints for ``query`` scoped to the
        active document's memory view. Implementations MUST be pure
        / synchronous / read-only."""
        ...


class NoOpAugmentationProvider:
    """Trivial provider that returns empty hints. Wired as the
    default when no domain pack is selected."""

    def hints_for(
        self,
        memory_view: "DocumentMemoryView",
        query: str,
    ) -> AugmentationHints:
        return AugmentationHints(source="disabled")


class DomainPackAugmentationProvider:
    """Domain-pack-backed default. Reads terminology + retrieval
    hints from the active ``DomainPack.extraction_hints``.

    Stateless after construction. The pack is captured by reference
    so a deployment-wide pack swap (e.g. a yaml reload) requires a
    new provider instance — by design, retrieval reads a stable
    snapshot of the domain config per request.

    The provider's ``hints_for`` ignores the ``query`` argument for
    now: returning all pack terms is the simplest useful surface and
    callers cap expansion size themselves. A future variant can
    intersect terms with question tokens; the contract stays the
    same.
    """

    def __init__(self, *, pack: "DomainPack | None") -> None:
        self._pack = pack

    def hints_for(
        self,
        memory_view: "DocumentMemoryView",
        query: str,
    ) -> AugmentationHints:
        if not is_augmentation_enabled():
            return AugmentationHints(source="disabled")
        if self._pack is None:
            return AugmentationHints(source="disabled")
        hints = getattr(self._pack, "extraction_hints", None)
        if hints is None:
            return AugmentationHints(source="domain_pack")
        terms = tuple(getattr(hints, "terminology_hints", ()) or ())
        retrieval_hints = tuple(
            getattr(hints, "retrieval_hints", ()) or ()
        )
        # The pack's ``terminology_hints`` are operator-readable
        # glossary entries; ``retrieval_hints`` are "look this up
        # too" suggestions. We surface the union as recommended
        # expansions so callers don't have to know which field
        # carries which intent.
        # Aliases land in Phase 4 (entity normalization). For now
        # the tuple stays empty — the FE renders "0 aliases" honestly.
        return AugmentationHints(
            domain_terms=terms,
            aliases=(),
            recommended_expansions=tuple(
                dict.fromkeys(list(terms) + list(retrieval_hints))
            ),
            source="domain_pack",
        )
