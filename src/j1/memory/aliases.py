"""Entity alias / normalization strategy.

Phase-4 surface that lets retrieval (and future query expansion)
resolve a user-mentioned form to a domain-known canonical entity +
its alternate spellings — WITHOUT mutating the RAGAnything graph or
hard-coding any domain-specific strings in core.

Two sources are supported:

  1. **Static, pack-shipped aliases** — read from
     ``DomainPack.extraction_hints.entity_aliases``. The pack is the
     compiled snapshot of "what this domain knows" and ships with
     the deployment. Every entry carries
     ``source="domain_config"``.

  2. **Enrichment-derived aliases** (optional, Phase-4 stub) —
     produced by Domain Enrichment as augmentation artifacts.
     Returned with ``source="domain_enrichment"`` so callers can
     filter / weight by source. The wire shape exists; the
     enrichment producer wiring is deferred.

The resolver is read-only, pure, and never reaches into RAGAnything
internals. Future graph-mutation strategies (when a stable
RAGAnything API exists) plug in as a third source without changing
the public shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from j1.domains.models import (
    ENTITY_ALIAS_SOURCE_DOMAIN_CONFIG,
    ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT,
    EntityAlias,
)

if TYPE_CHECKING:
    from j1.domains.models import DomainPack


__all__ = [
    "AliasResolver",
    "AliasResolution",
    "EntityAlias",  # re-export for the seam
    "ENTITY_ALIAS_SOURCE_DOMAIN_CONFIG",
    "ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT",
]


# Phase-4 cap on how many expansion forms one alias entry surfaces.
# Retrieval-side expansion is bounded separately by
# ``compute_query_expansion``; this is the per-entry guardrail so a
# single mis-configured pack entry can't fan out into hundreds of
# lookup terms. Real packs ship single-digit alias counts per
# canonical.
_MAX_FORMS_PER_ENTRY = 12


@dataclass(frozen=True)
class AliasResolution:
    """Outcome of a single resolve call.

    Pure data. ``matches`` is the set of ``EntityAlias`` entries
    whose canonical OR any alias matched (case-insensitive). The
    convenience ``all_forms`` collapses every match into a flat
    de-duplicated tuple of strings — what retrieval expansion
    actually consumes."""

    query: str
    matches: tuple[EntityAlias, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.matches

    def all_forms(self) -> tuple[str, ...]:
        """Every form across every match, de-duplicated, preserving
        first-seen order. Callers cap further if needed."""
        seen: dict[str, None] = {}
        for entry in self.matches:
            for form in entry.all_forms():
                if form and form not in seen:
                    seen[form] = None
        return tuple(seen.keys())


class AliasResolver:
    """Resolves a query against a layered set of alias sources.

    Construction order matters: the FIRST source wins for a given
    surface form, so static pack aliases (highest authority) come
    before enrichment-derived ones. Both sources can carry the same
    canonical without conflict — duplicates collapse in
    ``AliasResolution.all_forms``.

    Stateless after construction. Thread-safe (read-only).
    """

    def __init__(
        self,
        *,
        pack: "DomainPack | None" = None,
        enrichment_aliases: Iterable[EntityAlias] = (),
    ) -> None:
        # Order = precedence. Pack-static aliases come first; each
        # has ``source=domain_config``. Enrichment aliases are
        # appended after with ``source=domain_enrichment``.
        self._entries: tuple[EntityAlias, ...] = tuple(
            list(_pack_aliases(pack)) + list(enrichment_aliases)
        )

    @property
    def entries(self) -> tuple[EntityAlias, ...]:
        return self._entries

    def resolve(self, query: str) -> AliasResolution:
        """Return every entry whose canonical OR any alias matches
        the query (case-insensitive, whitespace-trimmed)."""
        if not query:
            return AliasResolution(query=query, matches=())
        needle = query.strip().lower()
        if not needle:
            return AliasResolution(query=query, matches=())
        matches: list[EntityAlias] = []
        for entry in self._entries:
            forms = entry.all_forms()[:_MAX_FORMS_PER_ENTRY]
            if any(form.lower() == needle for form in forms):
                matches.append(entry)
        return AliasResolution(query=query, matches=tuple(matches))

    def expand_terms(
        self, terms: Iterable[str],
    ) -> tuple[str, ...]:
        """Bulk variant: return the union of forms for every term
        in ``terms`` that hits an alias entry. Used by query-time
        expansion when the retriever has already tokenised the
        question. De-duplicated, order-preserving."""
        seen: dict[str, None] = {}
        for term in terms:
            resolution = self.resolve(term)
            for form in resolution.all_forms():
                if form not in seen:
                    seen[form] = None
        return tuple(seen.keys())


def _pack_aliases(pack: "DomainPack | None") -> tuple[EntityAlias, ...]:
    """Read static aliases off the pack. Returns an empty tuple when
    no pack is selected — generic deployments see no aliases."""
    if pack is None:
        return ()
    hints = getattr(pack, "extraction_hints", None)
    if hints is None:
        return ()
    return tuple(getattr(hints, "entity_aliases", ()) or ())
