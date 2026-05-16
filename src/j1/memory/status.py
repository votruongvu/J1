"""Knowledge Memory status projection — Phase 3B.

Compact read-only view over the active snapshot's
`knowledge_memory` artifact metadata. Used by:

  * REST endpoint ``GET /documents/{id}/knowledge-memory`` so the
    FE can render a small status section on the Document Detail
    page without rolling its own artifact-registry query.
  * Future final-ingestion-report aggregation (deferred to Phase 3B
    follow-up if attempt aggregation needs more invasive workflow
    changes).

Hard contract:

  * **Read-only.** No mutation, no LLM, no service-side build.
    The projection reads the artifact registry, filters to active
    snapshot artifacts, and returns a compact DTO.
  * **Snapshot-scoped.** Only considers the artifact matching the
    document's `active_snapshot_id`. Superseded rows
    (`metadata.search_state == "superseded"`) are explicitly
    excluded — that's the snapshot-isolation guarantee Phase 2
    established.
  * **Stable status vocabulary.** Five values:
    ``not_built``, ``base_compile_only``,
    ``updated_with_domain_insights``, ``failed``, ``unknown``.
    Add values, don't rename — dashboards filter on these.

Status derivation:

  * No active snapshot → ``not_built``.
  * No active ``knowledge_memory`` artifact for the snapshot →
    ``not_built``.
  * Artifact present + ``metadata.includes_domain_insights`` is
    truthy → ``updated_with_domain_insights``.
  * Artifact present + ``includes_domain_insights`` falsey (or
    absent) → ``base_compile_only``.
  * Multiple active artifacts for the same snapshot (data corruption
    — Phase 2's supersede sweep should prevent this; defensive
    branch only) → ``unknown``.

Phase 3B does NOT track failed-attempt persistence — Phase 3A
emits failures via the structured log event only, not as
durable artifacts. If a future phase persists failure records,
this resolver gains a ``failed`` branch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


_log = logging.getLogger(__name__)


# ---- Status vocabulary -----------------------------------------


STATUS_NOT_BUILT = "not_built"
STATUS_BASE_COMPILE_ONLY = "base_compile_only"
STATUS_UPDATED_WITH_DOMAIN_INSIGHTS = "updated_with_domain_insights"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"


_VALID_STATUSES: frozenset[str] = frozenset({
    STATUS_NOT_BUILT,
    STATUS_BASE_COMPILE_ONLY,
    STATUS_UPDATED_WITH_DOMAIN_INSIGHTS,
    STATUS_FAILED,
    STATUS_UNKNOWN,
})


# ---- Status DTO ------------------------------------------------


@dataclass(frozen=True)
class KnowledgeMemoryStatus:
    """Compact read-only status returned by the resolver.

    Field semantics:

      * `status` — one of the `STATUS_*` constants above.
      * `document_id` / `snapshot_id` — lineage tuple. `snapshot_id`
        is `None` only on the `not_built` no-active-snapshot path.
      * `artifact_id` — the active memory artifact's id, or `None`
        on `not_built` / `failed`.
      * `entry_count` — read from `metadata.entry_count` when
        available. Zero on `not_built`.
      * `includes_domain_insights` — convenience boolean (also
        encoded in `status`; the FE uses both shapes).
      * `last_trigger` — `after_compile / after_domain_enrichment /
        manual / None`. None on `not_built`; also None when an old
        Phase 2 artifact predates the trigger stamp.
      * `last_built_at` — ISO 8601 string of `created_at` (or
        `updated_at` when available). Format-stable across the
        wire.
      * `warnings` — passes through any warnings the artifact
        metadata carried.
    """

    status: str = STATUS_NOT_BUILT
    document_id: str = ""
    snapshot_id: str | None = None
    artifact_id: str | None = None
    entry_count: int = 0
    includes_domain_insights: bool = False
    last_trigger: str | None = None
    last_built_at: str | None = None
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """Wire shape — used by the REST projection. camelCase
        casing applied at the adapter boundary, NOT here."""
        return {
            "status": self.status,
            "document_id": self.document_id,
            "snapshot_id": self.snapshot_id,
            "artifact_id": self.artifact_id,
            "entry_count": self.entry_count,
            "includes_domain_insights": self.includes_domain_insights,
            "last_trigger": self.last_trigger,
            "last_built_at": self.last_built_at,
            "warnings": list(self.warnings),
        }


# ---- Resolver --------------------------------------------------


def resolve_knowledge_memory_status(
    *,
    ctx,
    document_id: str,
    active_snapshot_id: str | None,
    artifact_registry,
) -> KnowledgeMemoryStatus:
    """Look up the active `knowledge_memory` artifact for the
    document's current snapshot and project a compact status DTO.

    `active_snapshot_id=None` short-circuits to `not_built` — the
    caller (REST endpoint / final report builder) already has
    the document record and reads `active_snapshot_id` from it,
    so we accept it as an arg rather than reaching back into the
    source registry.

    Defensive across registry variants:
      * Registries lacking `list_artifacts` return `not_built`
        with a single warning rather than crashing.
      * Per-record metadata access uses `getattr` with defaults
        so an `ArtifactRecord` shape change doesn't break the
        projection.
    """
    if not active_snapshot_id:
        return KnowledgeMemoryStatus(
            status=STATUS_NOT_BUILT,
            document_id=document_id,
            snapshot_id=None,
        )

    list_artifacts = getattr(artifact_registry, "list_artifacts", None)
    if not callable(list_artifacts):
        _log.debug(
            "artifact_registry has no list_artifacts; status=not_built"
        )
        return KnowledgeMemoryStatus(
            status=STATUS_NOT_BUILT,
            document_id=document_id,
            snapshot_id=active_snapshot_id,
        )

    try:
        records = list_artifacts(ctx, kind="knowledge_memory")
    except TypeError:
        try:
            records = [
                r for r in list_artifacts(ctx)
                if getattr(r, "kind", None) == "knowledge_memory"
            ]
        except Exception:  # noqa: BLE001 — best-effort projection
            return KnowledgeMemoryStatus(
                status=STATUS_NOT_BUILT,
                document_id=document_id,
                snapshot_id=active_snapshot_id,
            )
    except Exception:  # noqa: BLE001
        return KnowledgeMemoryStatus(
            status=STATUS_NOT_BUILT,
            document_id=document_id,
            snapshot_id=active_snapshot_id,
        )

    active = _select_active_memory(
        records,
        document_id=document_id,
        snapshot_id=active_snapshot_id,
    )
    if not active:
        return KnowledgeMemoryStatus(
            status=STATUS_NOT_BUILT,
            document_id=document_id,
            snapshot_id=active_snapshot_id,
        )

    # Multiple active rows for the same snapshot is a data-quality
    # problem — Phase 2's supersede sweep should prevent it. Defensive
    # branch surfaces it via `unknown` so the FE doesn't render
    # confidently from a corrupted state.
    if len(active) > 1:
        return KnowledgeMemoryStatus(
            status=STATUS_UNKNOWN,
            document_id=document_id,
            snapshot_id=active_snapshot_id,
            warnings=(
                f"multiple_active_memory_artifacts:{len(active)}",
            ),
        )

    record = active[0]
    metadata = dict(getattr(record, "metadata", None) or {})
    includes = bool(metadata.get("includes_domain_insights", False))
    status = (
        STATUS_UPDATED_WITH_DOMAIN_INSIGHTS
        if includes else STATUS_BASE_COMPILE_ONLY
    )
    entry_count_raw = metadata.get("entry_count")
    try:
        entry_count = int(entry_count_raw) if entry_count_raw is not None else 0
    except (TypeError, ValueError):
        entry_count = 0

    return KnowledgeMemoryStatus(
        status=status,
        document_id=document_id,
        snapshot_id=active_snapshot_id,
        artifact_id=getattr(record, "artifact_id", None),
        entry_count=entry_count,
        includes_domain_insights=includes,
        last_trigger=_str_or_none(metadata.get("trigger")),
        last_built_at=_iso(getattr(record, "updated_at", None))
        or _iso(getattr(record, "created_at", None)),
        warnings=_warnings_from_metadata(metadata),
    )


# ---- Helpers ---------------------------------------------------


def _select_active_memory(
    records,
    *,
    document_id: str,
    snapshot_id: str,
) -> list:
    """Filter to memory artifacts for this (document, snapshot)
    where `metadata.search_state == "active"` (or unset)."""
    out: list = []
    for record in records:
        meta = dict(getattr(record, "metadata", None) or {})
        if meta.get("document_id") != document_id:
            continue
        if meta.get("snapshot_id") != snapshot_id:
            continue
        state = meta.get("search_state") or "active"
        if state != "active":
            continue
        out.append(record)
    return out


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _iso(value: Any) -> str | None:
    """Convert a datetime / string to ISO-8601 string. Returns
    None on missing or unrecognised values so the wire field stays
    typed."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _warnings_from_metadata(metadata: dict[str, Any]) -> tuple[str, ...]:
    """Best-effort extraction of warning strings from artifact
    metadata. The Phase 2 persist seam doesn't yet stamp
    warnings — when it does (or when an upstream pipeline adds
    them), they surface here without code changes."""
    raw = metadata.get("warnings")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(w) for w in raw if w)
