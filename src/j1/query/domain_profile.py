"""DomainProfile — the narrow read interface the orchestrator core
consults for domain-specific knowledge.

The core stays domain-neutral. Anything that's specific to a
deployed domain — stage vocabulary, field synonyms, artifact-kind
priorities — comes from a profile. Profiles are constructed once
per project (from the ``DomainPack`` selected during ingestion)
and threaded through the orchestrator.

Important: this module defines the *shape* of a profile, not any
particular profile. Domain packs live under ``j1/domains/`` and
feed values into ``DomainProfile`` at wire time. The domain-
purity guard means this file MUST NOT name any specific deployed
domain — no domain nouns or vocabulary tokens anywhere here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from j1.query.query_plan import Intent


@dataclass(frozen=True)
class StageVocabulary:
    """A named stage in a stage-progression query.

    ``aliases`` are the surface forms the planner accepts in the user
    question (e.g. "60% design", "60 percent design", "60pct design").
    The orchestrator normalises matches to ``canonical`` so downstream
    grouping is consistent."""

    canonical: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldVocabulary:
    """A named field a query can request.

    ``aliases`` lets "deliverables" / "submittals" / "outputs" all
    resolve to the same canonical group. ``artifact_kinds`` lists
    enriched artifact kinds that typically carry this field — used
    by the artifact-lookup route to short-circuit retrieval when an
    enriched overlay exists."""

    canonical: str
    aliases: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainProfile:
    """The narrow interface the orchestrator core reads from. Empty
    defaults are valid — a project without a domain profile gets
    generic behaviour.

    The classifier, evidence builder, and synthesizer ALL read this
    profile. Nothing else in ``j1/query/`` reaches into domain
    packs directly.
    """

    domain_id: str = ""
    # Stage vocabulary for stage_progression questions. When empty,
    # the classifier still detects stage_progression from generic
    # ordinal patterns ("60% / 90% / 100%") but answer-shape
    # grouping uses the raw matched phrases.
    stages: tuple[StageVocabulary, ...] = ()
    # Field vocabulary the question may request. The classifier
    # populates ``QueryPlan.requested_fields`` with canonical names
    # from this list. Empty profile → no canonical mapping; the
    # planner uses the raw noun phrases from the question.
    fields: tuple[FieldVocabulary, ...] = ()
    # Per-intent artifact-kind priority. Keys are intents, values
    # are ordered tuples (highest priority first). The evidence
    # builder uses these to prefer enriched overlays over raw
    # chunks for an intent. Empty mapping → all kinds equal.
    artifact_priority: Mapping[Intent, tuple[str, ...]] = field(
        default_factory=dict,
    )
    # Sufficiency-policy overrides keyed by intent. Each value is a
    # partial dict matching ``SufficiencyPolicy``'s fields. The
    # default policy still ships from the orchestrator core; this
    # only carries the overrides.
    sufficiency_overrides: Mapping[Intent, Mapping[str, object]] = (
        field(default_factory=dict)
    )
    # Prompt hints the synthesizer prepends to its system prompt
    # for a given intent. Domain packs supply these so a domain
    # can ask for "always include units" or "always cite a
    # specific clause format" without the synthesizer hard-coding
    # the rule.
    prompt_hints: Mapping[Intent, str] = field(default_factory=dict)

    def stage_canonical(self, surface: str) -> str | None:
        """Look up the canonical stage name for a surface form.
        Returns ``None`` when the surface doesn't match any known
        stage — caller falls back to the raw match."""
        s = surface.strip().lower()
        if not s:
            return None
        for stage in self.stages:
            if s == stage.canonical.lower():
                return stage.canonical
            for alias in stage.aliases:
                if s == alias.lower():
                    return stage.canonical
        return None

    def field_canonical(self, surface: str) -> str | None:
        """Look up the canonical field name for a surface form."""
        s = surface.strip().lower()
        if not s:
            return None
        for f in self.fields:
            if s == f.canonical.lower():
                return f.canonical
            for alias in f.aliases:
                if s == alias.lower():
                    return f.canonical
        return None

    def field_artifact_kinds(self, canonical: str) -> tuple[str, ...]:
        """Enriched artifact kinds that carry the given field. Empty
        tuple when the field isn't mapped — caller falls back to
        generic retrieval."""
        for f in self.fields:
            if f.canonical == canonical:
                return f.artifact_kinds
        return ()


# Generic profile — no domain. Returned when no domain pack is
# configured. The orchestrator always has *some* profile so calls
# don't have to special-case ``profile is None``.
GENERIC_PROFILE = DomainProfile(domain_id="")


__all__ = [
    "DomainProfile",
    "FieldVocabulary",
    "GENERIC_PROFILE",
    "StageVocabulary",
]
