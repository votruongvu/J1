"""Phase 4 — Knowledge Memory query-side settings.

Two env knobs, both conservative defaults:

  * ``J1_QUERY_KNOWLEDGE_MEMORY_ENABLED`` — opt-in master flag for
    the memory-aware query path. Default **False** so production
    behaviour is unchanged until operators deliberately turn it
    on. The flag is intentionally distinct from
    ``J1_QUERY_EXPANSION_ENABLED`` so persistent-memory rollout
    can be staged independently of the on-demand augmentation
    layer.
  * ``J1_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES`` — cap on the number
    of `KnowledgeMemoryEntry` rows the provider returns per
    query. Caps the expansion-term blast radius + keeps the
    diagnostic block small. Default 8.

Tolerant parsing — malformed values fall back to the documented
defaults rather than crashing the query path. Mirrors the
convention in ``j1.processing.knowledge_memory_settings``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


__all__ = [
    "ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED",
    "ENV_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES",
    "ENV_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS",
    "ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_DOCUMENTS",
    "ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_ARTIFACTS",
    "ENV_QUERY_KNOWLEDGE_MEMORY_MAX_SOURCE_EVIDENCE",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_EXPANSION_TERMS",
    "DEFAULT_MAX_PROJECT_DOCUMENTS",
    "DEFAULT_MAX_PROJECT_ARTIFACTS",
    "DEFAULT_MAX_SOURCE_EVIDENCE",
    "KnowledgeMemoryQuerySettings",
    "load_knowledge_memory_query_settings",
]


ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED = "J1_QUERY_KNOWLEDGE_MEMORY_ENABLED"
ENV_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES = (
    "J1_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES"
)
# Phase 5A (2026-05-16): cap on the number of memory-derived
# expansion terms that actually broaden retrieval. The orchestrator
# applies a separate per-job variant cap downstream
# (``_MAX_EXPANSION_VARIANTS_PER_JOB`` = 4); this cap controls the
# UPSTREAM pool the orchestrator merges with the existing
# augmentation expansions before that per-job cap fires.
ENV_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS = (
    "J1_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS"
)
# Phase 5A patch (2026-05-16): caps that bound the project-active
# scope walk. ``MAX_PROJECT_DOCUMENTS`` limits how many unique
# documents the provider inspects per project query;
# ``MAX_PROJECT_ARTIFACTS`` is a defence-in-depth cap on the total
# number of ``knowledge_memory`` artifacts loaded (a Phase 2
# supersede sweep should keep this close to ``MAX_PROJECT_DOCUMENTS``
# but the explicit cap protects against corrupted-state explosions).
ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_DOCUMENTS = (
    "J1_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_DOCUMENTS"
)
ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_ARTIFACTS = (
    "J1_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_ARTIFACTS"
)
# Phase 5B (2026-05-16): cap on the number of source-evidence
# candidates the `KnowledgeMemoryEvidenceResolver` injects into the
# evidence pipeline from selected memory entries' source refs. Bounds
# how much memory-guided evidence can crowd the LLM context — keeps
# normal-route evidence dominant unless memory truly carries unique
# refs.
ENV_QUERY_KNOWLEDGE_MEMORY_MAX_SOURCE_EVIDENCE = (
    "J1_QUERY_KNOWLEDGE_MEMORY_MAX_SOURCE_EVIDENCE"
)


DEFAULT_MAX_ENTRIES = 8
DEFAULT_MAX_EXPANSION_TERMS = 8
DEFAULT_MAX_PROJECT_DOCUMENTS = 20
DEFAULT_MAX_PROJECT_ARTIFACTS = 20
DEFAULT_MAX_SOURCE_EVIDENCE = 8


@dataclass(frozen=True)
class KnowledgeMemoryQuerySettings:
    """Resolved query-side memory settings.

    `enabled` is the master switch. When False, the provider is
    short-circuited before any artifact lookup — zero overhead
    for deployments not opting in.

    `max_entries` caps the number of selected entries the
    provider returns per query. Diagnostic counters are still
    accurate when the cap fires (a `truncated` warning surfaces
    on the trace block).
    """

    enabled: bool = False
    max_entries: int = DEFAULT_MAX_ENTRIES
    # Phase 5A: cap on memory-derived expansion terms that
    # broaden retrieval. Independent from `max_entries` because
    # one selected entry can produce multiple expansion terms
    # (e.g. an alias entry yields the canonical name + every
    # alias synonym). When the merger truncates the pool it
    # stamps `expansion_terms_truncated=true` on the trace.
    max_expansion_terms: int = DEFAULT_MAX_EXPANSION_TERMS
    # Phase 5A patch: project-active scope caps.
    max_project_documents: int = DEFAULT_MAX_PROJECT_DOCUMENTS
    max_project_artifacts: int = DEFAULT_MAX_PROJECT_ARTIFACTS
    # Phase 5B (2026-05-16): cap on the number of resolved
    # source-evidence candidates injected per query. Keeps the
    # memory-guided evidence from crowding the LLM context; the
    # canonical retrieval routes remain the primary source.
    max_source_evidence: int = DEFAULT_MAX_SOURCE_EVIDENCE


def load_knowledge_memory_query_settings(
    env: dict[str, str] | None = None,
) -> KnowledgeMemoryQuerySettings:
    src = env if env is not None else os.environ
    return KnowledgeMemoryQuerySettings(
        enabled=_read_bool(
            src, ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, default=False,
        ),
        max_entries=_read_positive_int(
            src, ENV_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES,
            default=DEFAULT_MAX_ENTRIES,
        ),
        max_expansion_terms=_read_positive_int(
            src, ENV_QUERY_KNOWLEDGE_MEMORY_MAX_EXPANSION_TERMS,
            default=DEFAULT_MAX_EXPANSION_TERMS,
        ),
        max_project_documents=_read_positive_int(
            src, ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_DOCUMENTS,
            default=DEFAULT_MAX_PROJECT_DOCUMENTS,
        ),
        max_project_artifacts=_read_positive_int(
            src, ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_ARTIFACTS,
            default=DEFAULT_MAX_PROJECT_ARTIFACTS,
        ),
        max_source_evidence=_read_positive_int(
            src, ENV_QUERY_KNOWLEDGE_MEMORY_MAX_SOURCE_EVIDENCE,
            default=DEFAULT_MAX_SOURCE_EVIDENCE,
        ),
    )


def _read_bool(env, key: str, *, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _read_positive_int(env, key: str, *, default: int) -> int:
    raw = env.get(key)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
