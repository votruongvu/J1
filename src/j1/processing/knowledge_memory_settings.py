"""Knowledge Memory auto-build settings — Phase 3A.

Two independent env flags, both conservative defaults (False), so
Phase 2's manual-build behaviour stays the production default
until query integration ships in a later phase:

  * ``J1_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED`` — build the
    snapshot-scoped ``knowledge_memory`` artifact automatically
    after a successful compile.
  * ``J1_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT`` — rebuild the
    same artifact automatically after a successful post-compile
    Domain Enrichment, superseding the base-only build for the
    same snapshot.

The two flags are independent. A deployment can enable only the
post-enrichment rebuild (memory is built lazily after the first
enrichment) or only the post-compile build (memory exists from
the moment compile completes; enrichment doesn't refresh it).

Hard contract on this module:

  * No behavioural overrides beyond on/off — model selection,
    cost caps, etc. live elsewhere.
  * Tolerant parsing — malformed env values fall back to the
    documented default rather than raising.
  * Distinct from ``J1_ENABLE_MANUAL_BUILD_KNOWLEDGE_MEMORY``,
    which governs the manual REST action. Auto-build is a
    workflow-lifecycle concern; manual build is an operator
    surface concern. Both can be on at once.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


__all__ = [
    "ENV_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED",
    "ENV_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT",
    "KnowledgeMemoryLifecycleSettings",
    "load_knowledge_memory_lifecycle_settings",
]


# ---- Env-var names (single source of truth) -----------------------


ENV_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED = (
    "J1_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED"
)
ENV_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT = (
    "J1_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT"
)


# ---- Settings dataclass ------------------------------------------


@dataclass(frozen=True)
class KnowledgeMemoryLifecycleSettings:
    """Resolved auto-build / rebuild flags for one worker.

    Both default False — Phase 3A introduces the wiring but keeps
    the production behaviour identical to Phase 2 (manual-only)
    until query integration ships and we can prove the memory
    artifact pays for itself in answer quality.
    """

    auto_build_after_compile: bool = False
    rebuild_after_enrichment: bool = False

    def any_enabled(self) -> bool:
        """True iff at least one lifecycle hook is active.
        Useful for guard branches that want to short-circuit the
        whole memory-attempt path when both flags are off."""
        return self.auto_build_after_compile or self.rebuild_after_enrichment


# ---- Loader ------------------------------------------------------


def load_knowledge_memory_lifecycle_settings(
    env: dict[str, str] | None = None,
) -> KnowledgeMemoryLifecycleSettings:
    """Build a ``KnowledgeMemoryLifecycleSettings`` from ``env``
    (defaults to ``os.environ``). Tolerant — malformed values
    fall back to ``False`` (the documented default), not a crash.
    Mirrors the convention in
    [enrichment_settings.py](./enrichment_settings.py)."""
    source = env if env is not None else os.environ
    return KnowledgeMemoryLifecycleSettings(
        auto_build_after_compile=_read_bool(
            source, ENV_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED,
            default=False,
        ),
        rebuild_after_enrichment=_read_bool(
            source, ENV_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT,
            default=False,
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
