"""Domain query augmentation provider.

The provider is the seam where retrieval / synthesis can ask "what
does the active domain pack say about this query?" without reaching
into ``DomainPack`` internals.

Contract:

* Inputs come from a ``DocumentMemoryView`` (the Unified Memory
  projection) plus the user's question.
* Outputs are pure-data hints — terms, aliases, recommended query
  expansions. Callers decide whether to use them.
* No I/O. No LLM calls. No state.

Sources, in precedence order:

1. **Static domain pack hints** — ``DomainPack.extraction_hints``
   (``terminology_hints``, ``retrieval_hints``, ``entity_aliases``).
   Loaded from the pack registry at construction time. Phase 4
   adds ``entity_aliases`` to the surface.
2. **(Future)** Enrichment-generated aliases from the active
   snapshot's ``enriched.*`` artifacts. The shape exists; the
   enrichment producer wiring is deferred.

Feature flag: ``J1_DOMAIN_QUERY_AUGMENTATION_ENABLED`` (default
``true``). When disabled, the provider returns empty tuples for
every query so callers that wire the provider into retrieval can
A/B compare ON vs OFF without touching code.

Phase-4 also exposes ``compute_query_expansion`` — a small,
deterministic helper that caps expansion-term count and dedupes,
so retrieval-side consumers don't have to re-implement the same
guardrail.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from j1.memory.aliases import AliasResolver

if TYPE_CHECKING:
    from j1.domains.models import DomainPack
    from j1.memory.unified import DocumentMemoryView


__all__ = [
    "AugmentationHints",
    "DomainQueryAugmentationProvider",
    "DomainPackAugmentationProvider",
    "NoOpAugmentationProvider",
    "ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED",
    "MAX_QUERY_EXPANSION_TERMS",
    "compute_query_expansion",
    "is_augmentation_enabled",
]


# Hard cap on how many expanded terms retrieval may consume per
# query. Single-digit by design — wider expansion causes noisier
# retrieval and obscures the "did the alias help?" signal in
# diagnostics. Callers may pass a stricter cap; they MUST NOT
# raise it past this ceiling without product review.
MAX_QUERY_EXPANSION_TERMS = 8


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
    hints AND entity aliases from the active
    ``DomainPack.extraction_hints``.

    Stateless after construction. The pack is captured by reference
    so a deployment-wide pack swap (e.g. a yaml reload) requires a
    new provider instance — by design, retrieval reads a stable
    snapshot of the domain config per request.

    Phase-4 populates ``aliases`` from
    ``DomainPack.extraction_hints.entity_aliases``. Each entry
    surfaces as ``(canonical, alias)`` pairs so the FE can render
    diagnostics like "RFI → request for information" without
    knowing the source dataclass.
    """

    def __init__(self, *, pack: "DomainPack | None") -> None:
        self._pack = pack
        # Built once at construction so per-call ``hints_for`` is
        # pure dict lookup. The resolver is also what
        # ``compute_query_expansion`` queries — exposing it on the
        # provider lets future retrieval-stage code reuse the same
        # alias index without re-walking the pack.
        self._alias_resolver = AliasResolver(pack=pack)

    @property
    def alias_resolver(self) -> AliasResolver:
        return self._alias_resolver

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
        # Phase-4: surface the static entity-alias bundle as
        # ``(canonical, alias)`` pairs so the FE / diagnostics
        # consumer doesn't have to know the dataclass shape.
        alias_pairs: list[tuple[str, str]] = []
        for entry in self._alias_resolver.entries:
            for alt in entry.aliases:
                if alt and alt != entry.canonical_name:
                    alias_pairs.append((entry.canonical_name, alt))
        # The pack's ``terminology_hints`` are operator-readable
        # glossary entries; ``retrieval_hints`` are "look this up
        # too" suggestions; ``entity_aliases`` carry canonical/alias
        # forms. We surface the union as recommended expansions so
        # callers don't have to know which field carries which
        # intent — capped via ``compute_query_expansion`` at the
        # call site.
        alias_forms: list[str] = []
        for entry in self._alias_resolver.entries:
            alias_forms.extend(entry.all_forms())
        return AugmentationHints(
            domain_terms=terms,
            aliases=tuple(alias_pairs),
            recommended_expansions=tuple(
                dict.fromkeys(
                    list(terms)
                    + list(retrieval_hints)
                    + alias_forms
                )
            ),
            source="domain_pack",
        )


def compute_query_expansion(
    query: str,
    hints: AugmentationHints,
    *,
    max_terms: int = MAX_QUERY_EXPANSION_TERMS,
) -> tuple[str, ...]:
    """Deterministic, capped expansion for retrieval-side consumers.

    Returns a tuple of unique terms suitable for OR-style retrieval
    expansion. The original ``query`` is preserved at index 0 so
    callers can pass the whole tuple to a retriever that scores
    every term independently; the original is NEVER dropped by the
    cap.

    Cap precedence:

      * ``max_terms`` ≤ ``MAX_QUERY_EXPANSION_TERMS`` is honoured
        as-is.
      * ``max_terms`` > the module-level ceiling is silently clamped
        — the ceiling is a safety net against accidental retrieval
        blowups.

    Empty / whitespace queries return ``("",)`` (or ``()`` when even
    the query is missing) so the call site can branch without
    re-checking. Diagnostics consumers MAY render the returned
    tuple verbatim under ``expanded_terms``.
    """
    capped = min(max(max_terms, 1), MAX_QUERY_EXPANSION_TERMS)
    seen: dict[str, None] = {}
    if query:
        seen[query] = None
    # Diagnostics-oriented order: terminology (operator-readable)
    # first, then alias forms (machine-friendly). The cap is applied
    # AFTER the original query is in — the query itself never gets
    # evicted by the cap.
    for term in list(hints.domain_terms) + list(hints.recommended_expansions):
        if not term:
            continue
        if term not in seen:
            seen[term] = None
        if len(seen) >= capped:
            break
    return tuple(seen.keys())
